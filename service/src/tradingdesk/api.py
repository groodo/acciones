"""API HTTP local (solo localhost, autenticada con token).

Con datos simulados solo se pueden crear borradores simulados y cancelarlos.
Con ``TRADINGDESK_DATA_SOURCE=ibkr`` el estado, la cuenta y las posiciones salen
de la cuenta Paper de IBKR y ``POST /v1/orders/paper`` envía órdenes limitadas a
esa cuenta tras superar los controles de riesgo. No existe ninguna ruta hacia una
cuenta real. Señales y curva siguen siendo simuladas y así se etiquetan.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import REAL_ORDER_CEILING_EUR, Settings
from .alpaca import IEX_NOTE, AlpacaData, AlpacaError, HybridSource, load_keys, mask_key, save_keys
from .broker import BrokerAdapter, BrokerUnavailable, IbkrPaperBroker
from .db import Database
from .domain import (
    AccountSnapshot,
    EquityCurve,
    EquityPoint,
    Envelope,
    Freshness,
    KillSwitchRequest,
    KillSwitchState,
    Money,
    OrderEvent,
    OrderSide,
    OrderRecord,
    OrderState,
    Position,
    Signal,
    SimulatedOrderRequest,
    SystemStatus,
)
from .logging_setup import log_event
from .mock_data import MOCK_NOTICE, MockProvider, seed_mock_orders
from .orders import OrderError, OrderStore
from .paper_orders import (
    PaperOrderRequest,
    ProtectRequest,
    RiskRejection,
    check_protection_prices,
    free_shares,
    protection_ids,
    check_order_limits,
    check_price_band,
    check_real_order_value,
)
from .model_runner import Forecaster, KronosForecaster
from .preferences import MAX_WATCHLIST, PreferenceError, Preferences
from .safety import KillSwitch, market_is_open
from .signal_engine import SignalEngine

log = logging.getLogger("tradingdesk.api")

class WatchlistRequest(BaseModel):
    symbols: list[str] = Field(max_length=MAX_WATCHLIST * 2)


class AlpacaKeysRequest(BaseModel):
    key_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9]+$")
    secret_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9/+=]+$")


class OrderLimitRequest(BaseModel):
    max_real_order_eur: Decimal = Field(gt=0, max_digits=9, decimal_places=2)


ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}
CANCELLABLE = {OrderState.DRAFT, OrderState.RISK_APPROVED}
BROKER_CANCELLABLE = {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED}
SNAPSHOT_EVERY = timedelta(minutes=5)


def load_or_create_token(path: Path) -> str:
    """Token aleatorio guardado con permisos 0600. La app lo lee del mismo archivo.

    Pendiente (Hito B): mover a Keychain, tal como pide el PRD.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            os.chmod(path, 0o600)
            return token
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    return token


def create_app(settings: Settings, *, token: str | None = None, provider: MockProvider | None = None,
               db: Database | None = None, broker: BrokerAdapter | None = None,
               forecaster: Forecaster | None = None) -> FastAPI:
    settings.validate()
    db = db or Database(settings.db_path)
    db.migrate()
    token = token or load_or_create_token(settings.token_path)
    provider = provider or MockProvider()
    orders = OrderStore(db)
    kill_switch = KillSwitch(db)
    prefs = Preferences(db)

    def real_cap() -> Decimal:
        return prefs.max_real_order_eur(settings.max_real_order_eur, REAL_ORDER_CEILING_EUR)
    if broker is None and settings.data_source != "ibkr":
        # Las órdenes de ejemplo solo existen en el modo simulado; con IBKR no se mezclan con las reales.
        seed_mock_orders(orders, provider.now())
    if broker is None and settings.data_source == "ibkr":
        broker = IbkrPaperBroker(settings.ib_host, settings.ib_port, settings.ib_client_id, orders=orders,
                                 orders_enabled=settings.real_trading or (settings.paper_orders and not settings.real_account),
                                 account_kind="real" if settings.real_account else "paper",
                                 allow_real_orders=settings.real_trading)
    if broker is None:
        _record_snapshot(db, provider.account())
    engine: SignalEngine | None = None
    forecaster = forecaster or KronosForecaster(settings.kronos_dir)
    alpaca_holder: list[AlpacaData] = []

    def candle_source():
        keys = load_keys(settings.alpaca_keys_path)
        if keys and broker is not None:
            alpaca_holder[:] = [AlpacaData(*keys)]
            return HybridSource(broker, alpaca_holder[0])
        alpaca_holder.clear()
        return broker

    if broker is not None and hasattr(broker, "historical_candles"):
        engine = SignalEngine(db, candle_source(), forecaster, prefs.watchlist(list(settings.watchlist)),
                              interval_s=settings.signal_interval_s)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if broker is not None:
            broker.start()
        if engine is not None:
            engine.start()
        yield
        if engine is not None:
            engine.stop()
        if broker is not None:
            broker.stop()

    app = FastAPI(title="AI Trading Desk — servicio local", version=__version__, docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.state.db = db
    app.state.broker = broker
    app.state.engine = engine

    def envelope(data, notice: str = MOCK_NOTICE) -> Envelope:
        return Envelope(source="MOCK", is_mock=True, mode=settings.mode,
                        generated_at=datetime.now(timezone.utc), notice=notice, data=data)

    def ibkr_envelope(data, notice: str | None = None) -> Envelope:
        return Envelope(source="IBKR", is_mock=False, mode="live" if settings.real_account else settings.mode,
                        generated_at=datetime.now(timezone.utc), notice=notice, data=data)

    last_recorded: list[datetime] = []
    order_mode = "live" if settings.real_account else settings.mode
    record_lock = threading.Lock()

    def broker_snapshot():
        snap = broker.snapshot()
        if snap.link != "conectado" or snap.account is None:
            raise HTTPException(503, snap.detail)
        # Curva de patrimonio real: un punto cada 5 minutos mientras la app consulta la cuenta.
        now = datetime.now(timezone.utc)
        with record_lock:  # las peticiones llegan en paralelo: un solo punto por intervalo
            if not last_recorded or now - last_recorded[-1] >= SNAPSHOT_EVERY:
                _record_snapshot(db, snap.account, source="IBKR")
                last_recorded[:] = [now]
        return snap

    # Con IBKR todo lo que devuelve el servicio es real: no hay respuestas simuladas.
    respond = ibkr_envelope if broker is not None else envelope

    def quotes_notice(freshness: Freshness) -> str | None:
        if freshness is Freshness.DELAYED:
            return "Cotizaciones DIFERIDAS (unos 15 minutos): la cuenta no tiene suscripción de datos en tiempo real."
        if freshness is Freshness.UNAVAILABLE:
            return "Sin cotizaciones de mercado: los precios son los últimos que calcula IBKR para la cuenta."
        return None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "Host no permitido"}, status_code=403)
        # La app nativa no envía Origin; cualquier petición desde un navegador se rechaza.
        if request.headers.get("origin"):
            return JSONResponse({"detail": "Origen no permitido"}, status_code=403)
        if request.url.path != "/health":
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {token}"
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                log_event(log, "Petición sin autenticar rechazada", logging.WARNING, path=request.url.path)
                return JSONResponse({"detail": "No autorizado"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/status", response_model=Envelope[SystemStatus])
    def status():
        now = datetime.now(timezone.utc)
        ks = kill_switch.active
        is_open = market_is_open(now)
        market_detail = "Sesión regular 9:30–16:00 America/New_York (festivos no contemplados)"
        if broker is not None:
            snap = broker.snapshot()
            if settings.real_trading:
                alerts = [f"CUENTA REAL: órdenes con dinero real. Solo limitadas, con tu confirmación en cada una y "
                          f"un tope de {real_cap()} € por orden.",
                          "Las previsiones de Kronos son orientativas, no una recomendación."]
            elif settings.real_account:
                alerts = ["Cuenta REAL de IBKR en solo lectura: la app no puede enviar ni cancelar órdenes.",
                          "Las previsiones de Kronos son orientativas, no una recomendación."]
            elif broker.orders_enabled:
                alerts = [f"Órdenes Paper habilitadas: solo limitadas, con tu confirmación y un tope de "
                          f"{settings.max_order_value} por orden. Dinero ficticio."]
            else:
                alerts = ["Cuenta Paper de IBKR en solo lectura: no se envían órdenes al bróker."]
            if snap.link != "conectado":
                alerts.insert(0, snap.detail)
            elif snap.positions and (n := quotes_notice(snap.quotes)):
                alerts.insert(0, n)
            if ks:
                alerts.append("Operativa detenida: no se aceptan nuevas entradas.")
            return ibkr_envelope(SystemStatus(
                broker=snap.link, broker_detail=snap.detail,
                mode="live" if settings.real_account else settings.mode,
                mode_label="REAL · CON ÓRDENES" if settings.real_trading
                else "REAL · SOLO LECTURA" if settings.real_account else "PAPER",
                market="abierto" if is_open else "cerrado", market_detail=market_detail, quotes=snap.quotes,
                last_update=snap.updated_at or now, kill_switch_active=ks,
                trading_allowed=broker.orders_enabled and snap.link == "conectado" and not ks,
                alerts=alerts, service_version=__version__,
                max_order_value=(Money(amount=real_cap(), currency="EUR") if settings.real_trading
                                 else Money(amount=settings.max_order_value, currency="USD"))
                if broker.orders_enabled else None,
            ))
        alerts = ["Cuenta Paper simulada: las operaciones no utilizan dinero real.",
                  "Sin conexión con IBKR en esta versión: los importes son de ejemplo."]
        if ks:
            alerts.append("Operativa detenida: no se aceptan nuevas entradas.")
        return envelope(SystemStatus(
            broker="desconectado",
            broker_detail="Hito A: aún no hay integración con IB Gateway",
            mode=settings.mode,
            mode_label="SIMULACIÓN",
            market="abierto" if is_open else "cerrado",
            market_detail=market_detail,
            quotes=Freshness.SIMULATED,
            last_update=now,
            kill_switch_active=ks,
            trading_allowed=False,
            alerts=alerts,
            service_version=__version__,
        ))

    @app.get("/v1/account", response_model=Envelope[AccountSnapshot])
    def account():
        if broker is not None:
            snap = broker_snapshot()
            return ibkr_envelope(snap.account, quotes_notice(snap.quotes) if snap.positions else None)
        return envelope(provider.account())

    @app.get("/v1/positions", response_model=Envelope[list[Position]])
    def positions():
        if broker is not None:
            snap = broker_snapshot()
            return ibkr_envelope(with_protection(snap.positions), quotes_notice(snap.quotes) if snap.positions else None)
        return envelope(provider.positions())

    @app.get("/v1/equity-curve", response_model=Envelope[EquityCurve])
    def equity_curve(range: Literal["hoy", "7d", "30d", "todo"] = "30d"):  # noqa: A002
        if broker is not None:
            return ibkr_envelope(_real_equity_curve(db, range, "live" if settings.real_account else "paper"))
        return envelope(provider.equity_curve(range))

    @app.get("/v1/signals", response_model=Envelope[list[Signal]])
    def signals():
        if engine is not None:
            st = forecaster.status()
            notice = ("Previsiones de Kronos-small (local). Las frecuencias de las muestras no son probabilidades "
                      "calibradas ni una recomendación.") if st.installed else st.detail
            return ibkr_envelope(engine.signals(), notice)
        if broker is not None:
            return ibkr_envelope([], "Este conector de IBKR no da velas: no hay señales.")
        return envelope(provider.signals(),
                        notice=MOCK_NOTICE + " Las señales no proceden de ningún modelo.")

    @app.get("/v1/model")
    def model_status():
        st = forecaster.status()
        return {"installed": st.installed, "loaded": st.loaded, "model_id": st.model_id, "version": st.version,
                "device": st.device, "detail": st.detail, "last_latency_ms": st.last_latency_ms,
                "last_run": engine.last_run.isoformat() if engine and engine.last_run else None,
                "watchlist": engine.watchlist if engine else list(settings.watchlist)}

    @app.get("/v1/opportunities", response_model=Envelope[list[Signal]])
    def opportunities():
        """Oportunidades que el sistema encuentra fuera de tu lista (Nasdaq), ordenadas: primero «Comprar»."""
        if engine is None:
            return envelope([], notice=MOCK_NOTICE + " Sin IBKR no se exploran oportunidades.")
        when = engine.last_discovery.astimezone().strftime("%H:%M") if engine.last_discovery else None
        notice = engine.discovery_detail + (f" Última exploración: {when}." if when else "") + \
            " Son propuestas del modelo, no una recomendación de inversión."
        return ibkr_envelope(engine.opportunities(), notice)

    @app.get("/v1/watchlist")
    def get_watchlist():
        return {"symbols": engine.watchlist if engine else prefs.watchlist(list(settings.watchlist)),
                "max": MAX_WATCHLIST}

    @app.put("/v1/watchlist")
    def put_watchlist(req: WatchlistRequest):
        try:
            symbols = prefs.set_watchlist(req.symbols)
        except PreferenceError as exc:
            raise HTTPException(422, str(exc)) from exc
        if engine is not None:
            engine.set_watchlist(symbols)
        log_event(log, "Lista de seguimiento actualizada", symbols=",".join(symbols))
        return {"symbols": symbols, "max": MAX_WATCHLIST}

    @app.get("/v1/data-provider")
    def get_data_provider():
        keys = load_keys(settings.alpaca_keys_path)
        return {"provider": "alpaca" if keys else "ibkr", "configured": keys is not None,
                "key_id_masked": mask_key(keys[0]) if keys else None,
                "detail": IEX_NOTE if keys else "Velas de IBKR (diferidas sin suscripción de datos)."}

    @app.put("/v1/data-provider")
    def put_data_provider(req: AlpacaKeysRequest):
        try:
            AlpacaData(req.key_id, req.secret_key).bars("AAPL", 5, days=5)
        except AlpacaError as exc:
            raise HTTPException(422, str(exc)) from exc
        save_keys(settings.alpaca_keys_path, req.key_id, req.secret_key)
        if engine is not None:
            engine.source = candle_source()
        log_event(log, "Claves de Alpaca guardadas", key=mask_key(req.key_id))
        return get_data_provider()

    @app.delete("/v1/data-provider")
    def delete_data_provider():
        settings.alpaca_keys_path.unlink(missing_ok=True)
        if engine is not None:
            engine.source = candle_source()
        log_event(log, "Claves de Alpaca borradas")
        return get_data_provider()

    @app.get("/v1/order-limit")
    def get_order_limit():
        return {"max_real_order_eur": str(real_cap()), "ceiling_eur": str(REAL_ORDER_CEILING_EUR),
                "real_trading": settings.real_trading}

    @app.put("/v1/order-limit")
    def put_order_limit(req: OrderLimitRequest):
        try:
            value = prefs.set_max_real_order_eur(req.max_real_order_eur, REAL_ORDER_CEILING_EUR)
        except PreferenceError as exc:
            raise HTTPException(422, str(exc)) from exc
        log_event(log, "Tope por orden real actualizado", max_eur=str(value))
        return {"max_real_order_eur": str(value), "ceiling_eur": str(REAL_ORDER_CEILING_EUR),
                "real_trading": settings.real_trading}

    @app.get("/v1/orders", response_model=Envelope[list[OrderRecord]])
    def list_orders(open_only: bool = False):
        if broker is not None:
            return ibkr_envelope(orders.list(mode=order_mode, is_mock=False, open_only=open_only))
        return envelope(orders.list(mode=settings.mode, open_only=open_only))

    @app.get("/v1/orders/{client_order_id}/events", response_model=Envelope[list[OrderEvent]])
    def order_events(client_order_id: str):
        if orders.get(client_order_id) is None:
            raise HTTPException(404, "Orden no encontrada")
        return respond(orders.events(client_order_id))

    @app.post("/v1/orders/simulated", response_model=Envelope[OrderRecord], status_code=201)
    def create_simulated(req: SimulatedOrderRequest):
        if broker is not None:
            raise HTTPException(409, "Con IBKR conectado no se crean órdenes simuladas.")
        if kill_switch.active:
            raise HTTPException(423, "Operativa detenida: no se aceptan nuevas entradas.")
        try:
            order, created = orders.create_draft(
                client_order_id=req.client_order_id, mode=settings.mode, is_mock=True, symbol=req.symbol,
                side=req.side, quantity=req.quantity, limit_price=req.limit_price, currency=req.currency,
                note="Borrador SIMULADO: no se envía a ningún bróker",
            )
        except OrderError as exc:
            raise HTTPException(409, str(exc)) from exc
        log_event(log, "Borrador simulado", client_order_id=order.client_order_id, created=created)
        body = envelope(order).model_dump(mode="json")
        return JSONResponse(body, status_code=201 if created else 200)

    @app.post("/v1/orders/paper", response_model=Envelope[OrderRecord], status_code=201)
    def create_paper_order(req: PaperOrderRequest):
        if settings.real_account:
            raise HTTPException(409, "Hay una cuenta real conectada: no se envían órdenes Paper.")
        if broker is None or not broker.orders_enabled:
            raise HTTPException(409, "El envío de órdenes Paper no está habilitado (hace falta la fuente IBKR).")
        return place_order(req, live=False)

    @app.post("/v1/orders/real", response_model=Envelope[OrderRecord], status_code=201)
    def create_real_order(req: PaperOrderRequest):
        """Orden limitada REAL. Solo con la cuenta ``real-trading``, tu confirmación y el tope en euros."""
        if not settings.real_trading:
            raise HTTPException(409, "Las órdenes reales no están habilitadas: la cuenta real está en solo lectura.")
        if broker is None or not broker.orders_enabled or getattr(broker, "order_mode", "paper") != "live":
            raise HTTPException(409, "El conector de IBKR no tiene habilitadas las órdenes reales.")
        return place_order(req, live=True)

    def protective_orders(mode: str) -> list[OrderRecord]:
        """Ventas abiertas que de verdad comprometen acciones (las de una compra aún sin ejecutar no cuentan)."""
        open_orders = orders.list(mode=mode, is_mock=False, open_only=True)
        pending_parents = {o.client_order_id for o in open_orders if o.side is OrderSide.BUY and o.filled_quantity == 0}
        return [o for o in open_orders if o.parent_client_order_id not in pending_parents]

    def with_protection(positions: list[Position]) -> list[Position]:
        """Rellena «stop / objetivo» de cada posición con sus ventas de protección abiertas en IBKR."""
        if broker is None or not positions:
            return positions
        mode = "live" if settings.real_account else "paper"
        sells = [o for o in protective_orders(mode) if o.side is OrderSide.SELL]
        out = []
        for p in positions:
            mine = [o for o in sells if o.symbol == p.symbol and o.currency == p.currency]
            stops = [o.limit_price for o in mine if o.order_type == "STP"]
            targets = [o.limit_price for o in mine if o.order_type == "LMT" and o.client_order_id.endswith("-TP")]
            out.append(p.model_copy(update={"stop": max(stops) if stops else p.stop,
                                            "target": min(targets) if targets else p.target}))
        return out

    def create_protection(parent_id: str, *, mode: str, symbol: str, quantity: Decimal, currency: str,
                          take_profit: Decimal, stop_loss: Decimal, live: bool, bracket: bool) -> tuple[str, str]:
        tp_id, sl_id = protection_ids(parent_id)
        money = "dinero real" if live else "dinero ficticio"
        when = "cuando se ejecute la compra" if bracket else "de acciones que ya tienes"
        for child_id, order_type, price, note in (
            (tp_id, "LMT", take_profit, f"Venta de ganancia máxima {when} ({money})"),
            (sl_id, "STP", stop_loss, f"Stop de pérdida máxima {when} ({money})"),
        ):
            orders.create_draft(
                client_order_id=child_id, mode=mode, is_mock=False, symbol=symbol, side=OrderSide.SELL,
                quantity=quantity, limit_price=price, currency=currency, note=note,
                allow_live=live and settings.real_trading, order_type=order_type,
                parent_client_order_id=parent_id if bracket else None,
            )
        return tp_id, sl_id

    def order_route() -> bool:
        """True si las órdenes van a la cuenta real; lanza 409 si no se pueden enviar órdenes."""
        if settings.real_account:
            if not settings.real_trading:
                raise HTTPException(409, "Las órdenes reales no están habilitadas: la cuenta real está en solo lectura.")
            if broker is None or not broker.orders_enabled or getattr(broker, "order_mode", "paper") != "live":
                raise HTTPException(409, "El conector de IBKR no tiene habilitadas las órdenes reales.")
            return True
        if broker is None or not broker.orders_enabled:
            raise HTTPException(409, "El envío de órdenes Paper no está habilitado (hace falta la fuente IBKR).")
        return False

    @app.post("/v1/orders/protect", response_model=Envelope[list[OrderRecord]], status_code=201)
    def protect_position(req: ProtectRequest):
        """Venta de ganancia máxima + stop de pérdida máxima (OCA) para acciones que ya tienes."""
        live = order_route()
        mode = "live" if live else "paper"
        tp_id, sl_id = protection_ids(req.client_order_id)
        existing = [orders.get(tp_id), orders.get(sl_id)]
        if all(existing):
            same = all((o.symbol, o.quantity) == (req.symbol, req.quantity) for o in existing) and \
                (existing[0].limit_price, existing[1].limit_price) == (req.take_profit_price, req.stop_loss_price)
            if not same or any(o.mode != mode for o in existing):
                raise HTTPException(409, "client_order_id ya usado con otros parámetros")
            return JSONResponse(ibkr_envelope(existing).model_dump(mode="json"), status_code=200)
        snap = broker_snapshot()
        try:
            if not req.confirmed:
                raise RiskRejection("Falta tu confirmación explícita para enviar la orden.")
            free = free_shares(req.symbol, req.currency, snap.positions, protective_orders(mode))
            if req.quantity > free:
                raise RiskRejection(f"Solo puedes proteger acciones que tengas libres: {free:f} de {req.symbol} "
                                    "(las que ya tienen una venta abierta no cuentan).")
            reference = broker.reference_price(req.symbol, req.currency)
            if reference is None or reference <= 0:
                raise RiskRejection(f"No hay precio de referencia para {req.symbol} ahora mismo.")
            check_protection_prices(reference, req.take_profit_price, req.stop_loss_price)
        except RiskRejection as exc:
            raise HTTPException(422, str(exc)) from exc
        except BrokerUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        create_protection(req.client_order_id, mode=mode, symbol=req.symbol, quantity=req.quantity,
                          currency=req.currency, take_profit=req.take_profit_price,
                          stop_loss=req.stop_loss_price, live=live, bracket=False)
        for i in (tp_id, sl_id):
            orders.transition(i, OrderState.RISK_APPROVED,
                              f"Controles superados: confirmada, {req.quantity} acciones libres, "
                              f"precios dentro del 50 % de la referencia ({reference})")
        log_event(log, "Protección aprobada", symbol=req.symbol, quantity=str(req.quantity),
                  take_profit=str(req.take_profit_price), stop_loss=str(req.stop_loss_price), mode=mode)
        submit_group(tp_id, sl_id, None)
        return JSONResponse(ibkr_envelope([orders.get(tp_id), orders.get(sl_id)]).model_dump(mode="json"),
                            status_code=201)

    def submit_group(tp_id: str, sl_id: str, parent_id: str | None) -> None:
        ids = [i for i in (parent_id, tp_id, sl_id) if i]
        try:
            broker.submit_protected(tp_id, sl_id, parent_id)
        except Exception as exc:  # noqa: BLE001
            if orders.get(ids[0]).state is OrderState.RISK_APPROVED:
                for i in ids:
                    orders.transition(i, OrderState.CANCELLED, f"No se pudo enviar: {exc}")
                raise HTTPException(503, f"No se pudo enviar la orden: {exc}") from exc
            # Llegó a SUBMITTING: queda en UNKNOWN y se reconciliará; no se reenvía.

    def place_order(req: PaperOrderRequest, *, live: bool):
        mode = "live" if live else settings.mode
        if kill_switch.active:
            raise HTTPException(423, "Operativa detenida: no se aceptan nuevas entradas.")
        existing = orders.get(req.client_order_id)
        if existing is not None:
            # Reintento de la misma petición: se devuelve la orden sin reenviarla.
            same = (existing.symbol, existing.side, existing.quantity, existing.limit_price) == (
                req.symbol, req.side, req.quantity, req.limit_price)
            if not same or existing.is_mock or existing.mode != mode:
                raise HTTPException(409, "client_order_id ya usado con otros parámetros")
            return JSONResponse(ibkr_envelope(existing).model_dump(mode="json"), status_code=200)
        snap = broker_snapshot()
        try:
            if live:
                check_order_limits(req, positions=snap.positions, max_order_value=req.quantity * req.limit_price)
                cap = real_cap()
                value_eur = check_real_order_value(req, snap.account, cap)
                limit_text = f"importe {value_eur:.2f} € ≤ {cap} €"
            else:
                check_order_limits(req, positions=snap.positions, max_order_value=settings.max_order_value)
                limit_text = f"importe ≤ {settings.max_order_value}"
            if req.protected:
                if req.side is not OrderSide.BUY:
                    raise RiskRejection("La venta automática de ganancia y pérdida solo se añade a una compra.")
                check_protection_prices(req.limit_price, req.take_profit_price, req.stop_loss_price)
            check_price_band(req, broker.reference_price(req.symbol, req.currency))
        except RiskRejection as exc:
            raise HTTPException(422, str(exc)) from exc
        except BrokerUnavailable as exc:
            raise HTTPException(503 if not str(exc).startswith("IBKR no reconoce") else 422, str(exc)) from exc
        order, _ = orders.create_draft(
            client_order_id=req.client_order_id, mode=mode, is_mock=False, symbol=req.symbol,
            side=req.side, quantity=req.quantity, limit_price=req.limit_price, currency=req.currency,
            note="Orden limitada REAL en IBKR (dinero real)" if live
            else "Orden limitada en la cuenta Paper de IBKR (dinero ficticio)",
            allow_live=live and settings.real_trading,
        )
        orders.transition(order.client_order_id, OrderState.RISK_APPROVED,
                          f"Controles superados: confirmada, {limit_text}, precio dentro del 5 % de la referencia")
        log_event(log, "Orden REAL aprobada" if live else "Orden Paper aprobada",
                  client_order_id=order.client_order_id, symbol=req.symbol,
                  side=req.side.value, quantity=str(req.quantity), limit=str(req.limit_price),
                  take_profit=str(req.take_profit_price), stop_loss=str(req.stop_loss_price))
        if req.protected:
            tp_id, sl_id = create_protection(
                order.client_order_id, mode=mode, symbol=req.symbol, quantity=req.quantity, currency=req.currency,
                take_profit=req.take_profit_price, stop_loss=req.stop_loss_price, live=live, bracket=True)
            for i in (tp_id, sl_id):
                orders.transition(i, OrderState.RISK_APPROVED,
                                  "Controles superados con la compra: ganancia por encima y pérdida por debajo del límite")
            submit_group(tp_id, sl_id, order.client_order_id)
            body = ibkr_envelope(orders.get(order.client_order_id)).model_dump(mode="json")
            return JSONResponse(body, status_code=201)
        try:
            broker.submit(order.client_order_id)
        except Exception as exc:  # noqa: BLE001
            current = orders.get(order.client_order_id)
            if current.state is OrderState.RISK_APPROVED:
                orders.transition(order.client_order_id, OrderState.CANCELLED, f"No se pudo enviar: {exc}")
                raise HTTPException(503, f"No se pudo enviar la orden: {exc}") from exc
            # Llegó a SUBMITTING: queda en UNKNOWN y se reconciliará; no se reenvía.
        body = ibkr_envelope(orders.get(order.client_order_id)).model_dump(mode="json")
        return JSONResponse(body, status_code=201)

    @app.post("/v1/orders/{client_order_id}/cancel", response_model=Envelope[OrderRecord])
    def cancel(client_order_id: str):
        order = orders.get(client_order_id)
        if order is None:
            raise HTTPException(404, "Orden no encontrada")
        if not order.is_mock and order.state in BROKER_CANCELLABLE:
            if broker is None or not broker.orders_enabled:
                raise HTTPException(409, "Sin conexión de órdenes con IBKR")
            try:
                broker.cancel(client_order_id)
            except BrokerUnavailable as exc:
                raise HTTPException(503, str(exc)) from exc
            # La confirmación de IBKR llega por su cuenta y moverá la orden a CANCELLED.
            return JSONResponse(ibkr_envelope(order).model_dump(mode="json"), status_code=202)
        if order.state not in CANCELLABLE:
            raise HTTPException(409, f"No se puede cancelar desde {order.state.value} en esta versión")
        return envelope(orders.transition(client_order_id, OrderState.CANCELLED, "Cancelada por el usuario"))

    def current_positions() -> list[Position]:
        if broker is None:
            return provider.positions()
        return broker.snapshot().positions

    def kill_switch_view() -> KillSwitchState:
        st = kill_switch.state()
        return KillSwitchState(
            active=st["active"],
            reason=st["reason"],
            changed_at=datetime.fromisoformat(st["changed_at"]) if st["changed_at"] else None,
            open_orders=orders.list(mode=order_mode, open_only=True,
                                    is_mock=False if broker is not None else None),
            open_positions=current_positions(),
            message=("Operativa detenida. Se bloquean nuevas entradas; las órdenes y posiciones listadas "
                     "siguen activas y no se liquidan automáticamente.") if st["active"]
            else ("Operativa habilitada." if broker is not None else "Operativa habilitada en simulación."),
        )

    @app.get("/v1/kill-switch", response_model=Envelope[KillSwitchState])
    def get_kill_switch():
        return respond(kill_switch_view())

    @app.post("/v1/kill-switch", response_model=Envelope[KillSwitchState])
    def set_kill_switch(req: KillSwitchRequest):
        kill_switch.set(req.active, req.reason)
        log_event(log, "Kill switch actualizado", active=req.active, reason=req.reason)
        return respond(kill_switch_view())

    return app


def _record_snapshot(db: Database, snapshot: AccountSnapshot, source: str = "MOCK") -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO account_snapshots (taken_at, mode, source, is_mock, payload) VALUES (?, ?, ?, ?, ?)",
            (snapshot.timestamp.isoformat(), snapshot.mode, source, int(source == "MOCK"),
             snapshot.model_dump_json()),
        )


def _real_equity_curve(db: Database, range_: str, mode: str, now: datetime | None = None) -> EquityCurve:
    """Curva con los valores liquidativos reales que ha ido guardando el servicio (sin inventar histórico)."""
    now = now or datetime.now(timezone.utc)
    if range_ == "hoy":
        since = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        since = now - timedelta(days={"7d": 7, "30d": 30, "todo": 36500}[range_])
    rows = db.query("SELECT taken_at, payload FROM account_snapshots WHERE is_mock = 0 AND mode = ? "
                    "ORDER BY taken_at", (mode,))
    points = []
    for r in rows:
        taken = datetime.fromisoformat(r["taken_at"])
        if taken < since:
            continue
        if points and points[-1].timestamp == taken:
            continue  # mismo instante registrado dos veces
        snap = AccountSnapshot.model_validate_json(r["payload"])
        points.append(EquityPoint(timestamp=taken, equity_eur=snap.net_liquidation_eur.amount,
                                  net_flows_eur=Decimal("0.00")))
    sufficient = len(points) >= 2
    message = ("Valor liquidativo de tu cuenta registrado por la app cada 5 minutos mientras está abierta. "
               "No descuenta ingresos ni retiradas.")
    if not sufficient:
        message = "Aún no hay suficientes puntos: la app guarda el valor de tu cuenta cada 5 minutos mientras está abierta."
    return EquityCurve(range=range_, sufficient=sufficient, message=message, points=points)
