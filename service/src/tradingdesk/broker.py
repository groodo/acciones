"""Conexión con IB Gateway.

Se leen saldo, posiciones y cotizaciones a través de la TWS API oficial
(biblioteca ``ib_async``). Hay dos tipos de conexión:

- Paper (por defecto): si está habilitado, se envían y cancelan órdenes limitadas.
  Si la cuenta conectada no es Paper (los identificadores Paper empiezan por
  ``DU``), se desconecta y no reintenta; además se comprueba otra vez antes de
  cada envío.
- Real (``account_kind="real"``): exige una cuenta que no sea Paper. Por defecto es
  de solo lectura (``readonly=True``). Solo con ``allow_real_orders=True`` (cuenta
  ``real-trading`` en la configuración) se envían órdenes limitadas ya confirmadas
  por el usuario y aprobadas por los controles de riesgo; ``submit`` comprueba de
  nuevo el tipo de cuenta y el modo de la orden antes de cada envío.

La API de IB es asíncrona; se ejecuta en un hilo propio con su bucle de eventos
y publica instantáneas inmutables que la API HTTP lee sin bloquear.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, Literal, Protocol

from .domain import AccountSnapshot, Freshness, FxRate, Money, OrderState, Position
from .logging_setup import log_event
from .orders import OrderStore
from .paper_orders import BrokerOrderStatus, mark_missing_as_unknown, sync_order

log = logging.getLogger("tradingdesk.broker")
# ib_async registra cada intento fallido como ERROR; ya lo resume nuestro aviso de reconexión.
logging.getLogger("ib_async.client").setLevel(logging.CRITICAL)


class _ScannerCancelNoise(logging.Filter):
    """IBKR responde con el error 162 al cerrar cada escaneo terminado: es normal, no un fallo."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "scanner subscription cancelled" not in record.getMessage()


logging.getLogger("ib_async.wrapper").addFilter(_ScannerCancelNoise())

CENT = Decimal("0.01")
REFRESH_SECONDS = 5
RECONNECT_SECONDS = (2, 5, 10, 30)
# Tipos de datos de mercado de la TWS API: 1 tiempo real, 2 congelados, 3 diferidos, 4 diferidos congelados.
_FRESHNESS_BY_TYPE = {1: Freshness.REAL_TIME, 2: Freshness.REAL_TIME, 3: Freshness.DELAYED, 4: Freshness.DELAYED}

BrokerLink = Literal["conectado", "desconectado", "reconectando"]


class NotPaperAccountError(RuntimeError):
    """La sesión de IB Gateway no es del tipo de cuenta configurado (Paper o real)."""


class BrokerUnavailable(RuntimeError):
    """No se puede hablar con IBKR ahora mismo (desconectado, envío desactivado, orden no lista)."""


class UnknownSymbol(BrokerUnavailable):
    """IBKR no reconoce el símbolo."""


def is_paper_account(account_id: str) -> bool:
    return account_id.upper().startswith("DU")


def mask_account(account_id: str) -> str:
    if len(account_id) <= 5:
        return "*" * len(account_id)
    prefix = "DU" if account_id.upper().startswith("DU") else account_id[:1]
    return prefix + "*" * (len(account_id) - len(prefix) - 3) + account_id[-3:]


def to_decimal(value: object) -> Decimal | None:
    """Convierte números de IB (float o texto) a Decimal; ``None`` si falta o no es finito."""
    if value is None or value == "":
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Quote:
    last: Decimal | None
    close: Decimal | None
    freshness: Freshness


@dataclass(frozen=True)
class BrokerSnapshot:
    link: BrokerLink
    detail: str
    account: AccountSnapshot | None = None
    positions: list[Position] = field(default_factory=list)
    quotes: Freshness = Freshness.UNAVAILABLE
    updated_at: datetime | None = None


class BrokerAdapter(Protocol):
    orders_enabled: bool

    def snapshot(self) -> BrokerSnapshot: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def reference_price(self, symbol: str, currency: str) -> Decimal | None: ...
    def submit(self, client_order_id: str) -> None: ...
    def cancel(self, client_order_id: str) -> None: ...


# --- Conversión de datos de IB a los contratos del PRD ------------------------------------------


def build_account(values: Iterable, *, account_id: str, daily_pnl: Decimal | None, quotes: Freshness,
                  now: datetime, mode: Literal["paper", "live"] = "paper") -> AccountSnapshot:
    """Construye ``AccountSnapshot`` a partir de ``AccountValue`` (account, tag, value, currency)."""
    base: dict[str, Decimal] = {}
    base_currency = None
    cash_by_currency: dict[str, Decimal] = {}
    fx_to_base: dict[str, Decimal] = {}
    for v in values:
        if v.account != account_id:
            continue
        amount = to_decimal(v.value)
        if amount is None:
            continue
        if v.tag == "ExchangeRate" and v.currency not in ("", "BASE"):
            fx_to_base[v.currency] = amount
        elif v.tag == "CashBalance" and v.currency not in ("", "BASE"):
            cash_by_currency[v.currency] = amount
        elif v.tag == "NetLiquidation" and v.currency not in ("", "BASE"):
            base_currency = v.currency
            base[v.tag] = amount
        elif v.tag in {"AvailableFunds", "BuyingPower", "UnrealizedPnL", "RealizedPnL", "GrossPositionValue",
                       "TotalCashValue"} and v.currency not in ("BASE",):
            base[v.tag] = amount
    if base_currency is None or "NetLiquidation" not in base:
        raise ValueError("IB Gateway aún no ha enviado el valor liquidativo de la cuenta")

    def money(tag: str) -> Money:
        return Money(amount=_q(base.get(tag, Decimal("0"))), currency=base_currency)

    nlv = base["NetLiquidation"]
    fx: FxRate | None = None
    if base_currency == "EUR":
        nlv_eur = nlv
        usd_rate = fx_to_base.get("USD")  # EUR por USD
        if usd_rate:
            fx = FxRate(pair="EUR.USD", rate=(Decimal(1) / usd_rate).quantize(Decimal("0.0001")),
                        source="IBKR (tipo de la cuenta)", as_of=now)
    else:
        eur_rate = fx_to_base.get("EUR")  # moneda base por EUR
        nlv_eur = nlv / eur_rate if eur_rate else None
        if eur_rate:
            fx = FxRate(pair=f"EUR.{base_currency}", rate=eur_rate.quantize(Decimal("0.0001")),
                        source="IBKR (tipo de la cuenta)", as_of=now)
    daily = daily_pnl if daily_pnl is not None else Decimal("0")
    prev = nlv - daily
    exposure = base.get("GrossPositionValue", Decimal("0"))
    unrealized = base.get("UnrealizedPnL", Decimal("0"))
    return AccountSnapshot(
        account_id_masked=mask_account(account_id),
        mode=mode,
        timestamp=now,
        base_currency=base_currency,
        net_liquidation=Money(amount=_q(nlv), currency=base_currency),
        net_liquidation_eur=Money(amount=_q(nlv_eur) if nlv_eur is not None else _q(nlv),
                                  currency="EUR" if nlv_eur is not None else base_currency),
        fx=fx,
        settled_cash=[Money(amount=_q(a), currency=c) for c, a in sorted(cash_by_currency.items())]
        or [money("TotalCashValue")],
        available_funds=money("AvailableFunds"),
        buying_power=money("BuyingPower"),
        unrealized_pnl=money("UnrealizedPnL"),
        realized_pnl=money("RealizedPnL"),
        realized_fees=Money(amount=Decimal("0.00"), currency=base_currency),
        daily_pnl=Money(amount=_q(daily), currency=base_currency),
        daily_pnl_pct=_q(daily / prev * 100) if prev else Decimal("0.00"),
        daily_pnl_basis=("P&L diario de IBKR / (valor liquidativo − P&L diario). Incluye aportaciones o retiradas "
                         "del día si las hubo.") if daily_pnl is not None
        else "IBKR aún no ha enviado el P&L diario.",
        invested_capital=Money(amount=_q(exposure - unrealized), currency=base_currency),
        exposure=Money(amount=_q(exposure), currency=base_currency),
        risk_budget=Money(amount=Decimal("0.00"), currency=base_currency),
        freshness=quotes,
    )


def build_positions(items: Iterable, quotes: dict[int, Quote]) -> list[Position]:
    """Convierte ``PortfolioItem`` de IB en ``Position``. La variación diaria sale del cierre anterior."""
    result = []
    for item in items:
        qty = to_decimal(item.position)
        if not qty:
            continue
        c = item.contract
        quote = quotes.get(c.conId)
        last = (quote.last if quote and quote.last is not None else None) or to_decimal(item.marketPrice)
        avg = to_decimal(item.averageCost) or Decimal("0")
        mult = to_decimal(getattr(c, "multiplier", "") or None) or Decimal("1")
        if mult != 1:
            avg = avg / mult
        change = None
        if quote and quote.close and last is not None:
            change = _q((last - quote.close) / quote.close * 100)
        result.append(Position(
            symbol=c.localSymbol or c.symbol,
            name=getattr(c, "description", "") or c.symbol,
            quantity=qty,
            currency=c.currency,
            avg_price=_q(avg),
            last_price=_q(last) if last is not None else Decimal("0.00"),
            unrealized_pnl=Money(amount=_q(to_decimal(item.unrealizedPNL) or Decimal("0")), currency=c.currency),
            change_pct=change,
        ))
    return sorted(result, key=lambda p: p.symbol)


def overall_freshness(quotes: Iterable[Quote]) -> Freshness:
    values = [q.freshness for q in quotes]
    if not values:
        return Freshness.UNAVAILABLE
    if all(v is Freshness.REAL_TIME for v in values):
        return Freshness.REAL_TIME
    if any(v in (Freshness.REAL_TIME, Freshness.DELAYED) for v in values):
        return Freshness.DELAYED
    return Freshness.UNAVAILABLE


# --- Conexión ------------------------------------------------------------------------------------


class IbkrPaperBroker:
    """Mantiene la conexión con IB Gateway Paper en un hilo propio.

    Con ``orders_enabled=False`` la conexión es de solo lectura (``readonly=True``) y cualquier intento de
    enviar una orden falla antes de llegar a IBKR.
    """

    def __init__(self, host: str, port: int, client_id: int, *, orders: OrderStore | None = None,
                 orders_enabled: bool = False, account_kind: Literal["paper", "real"] = "paper",
                 allow_real_orders: bool = False):
        self.host, self.port, self.client_id = host, port, client_id
        self.orders = orders
        self.account_kind = account_kind
        # Cuenta real: solo lectura salvo que la configuración lo permita de forma explícita.
        self.orders_enabled = orders_enabled and orders is not None and (
            account_kind == "paper" or allow_real_orders)
        self.order_mode = "live" if account_kind == "real" else "paper"
        self._lock = threading.Lock()
        self._snapshot = BrokerSnapshot(link="desconectado", detail="Conectando con IB Gateway…")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ib = None
        self._account: str | None = None
        self._errors: dict[int, str] = {}

    def snapshot(self) -> BrokerSnapshot:
        with self._lock:
            return self._snapshot

    def ready(self) -> bool:
        """Conectado y con la cuenta identificada."""
        ib = self._ib
        return ib is not None and ib.isConnected() and self._account is not None

    def _publish(self, snap: BrokerSnapshot) -> None:
        with self._lock:
            self._snapshot = snap

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="ibkr-paper", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # --- Órdenes (llamadas desde los hilos de la API HTTP) ------------------------------------------

    def _call(self, coro_factory, timeout: float = 15):
        loop, ib = self._loop, self._ib
        if loop is None or ib is None or not ib.isConnected() or self._account is None:
            raise BrokerUnavailable("IB Gateway no está conectado.")
        return asyncio.run_coroutine_threadsafe(coro_factory(ib), loop).result(timeout=timeout)

    def reference_price(self, symbol: str, currency: str) -> Decimal | None:
        """Último precio (o cierre anterior) del símbolo según IBKR, para validar el precio límite."""
        async def run(ib):
            contract = await self._qualify(ib, symbol, currency)
            ticker = ib.reqMktData(contract, "", False, False)
            try:
                for _ in range(30):
                    price = to_decimal(ticker.marketPrice()) or to_decimal(ticker.close)
                    if price:
                        return price
                    await asyncio.sleep(0.1)
                return None
            finally:
                ib.cancelMktData(contract)
        return self._call(run)

    def _account_matches(self) -> bool:
        account = self._account or ""
        return is_paper_account(account) if self.account_kind == "paper" else bool(account) and not is_paper_account(account)

    def submit(self, client_order_id: str) -> None:
        """Envía a IBKR una orden ya confirmada y aprobada por riesgo (RISK_APPROVED → SUBMITTING)."""
        if not self.orders_enabled:
            raise BrokerUnavailable("El envío de órdenes está desactivado en esta configuración.")

        async def run(ib):
            from ib_async import LimitOrder

            if not self._account_matches():
                raise NotPaperAccountError("La cuenta conectada no es la configurada: no se envía.")
            order = self.orders.get(client_order_id)
            if order is None or order.is_mock or order.mode != self.order_mode or order.state is not OrderState.RISK_APPROVED:
                raise BrokerUnavailable("La orden no está lista para enviarse.")
            contract = await self._qualify(ib, order.symbol, order.currency)
            ib_order = LimitOrder(order.side.value, float(order.quantity), float(order.limit_price),
                                  account=self._account, orderRef=client_order_id, tif="DAY")
            self.orders.transition(client_order_id, OrderState.SUBMITTING,
                                   "Enviando a IBKR (cuenta REAL)" if self.order_mode == "live" else "Enviando a IBKR Paper")
            try:
                trade = ib.placeOrder(contract, ib_order)
            except Exception as exc:
                # No sabemos si llegó: UNKNOWN y nunca se reenvía sola.
                self.orders.transition(client_order_id, OrderState.UNKNOWN, f"Error al enviar: {exc}")
                raise
            self.orders.set_broker_ids(client_order_id, trade.order.orderId, trade.order.permId or None)
            log_event(log, "Orden enviada", client_order_id=client_order_id, order_id=trade.order.orderId,
                      mode=self.order_mode)
        self._call(run)

    def cancel(self, client_order_id: str) -> None:
        if not self.orders_enabled:
            raise BrokerUnavailable("El envío de órdenes está desactivado en esta configuración.")

        async def run(ib):
            for trade in ib.openTrades():
                if trade.order.orderRef == client_order_id:
                    ib.cancelOrder(trade.order)
                    return
            raise BrokerUnavailable("IBKR no tiene esa orden abierta.")
        self._call(run)

    def historical_candles(self, symbol: str, bar_minutes: int, currency: str = "USD"):
        """Velas OHLCV cerradas de la sesión regular (últimos 5 días) para Kronos. Solo lectura."""
        from .forecast import Candle  # noqa: PLC0415

        async def run(ib):
            contract = await self._qualify(ib, symbol, currency)
            bars = await ib.reqHistoricalDataAsync(
                contract, endDateTime="", durationStr="5 D", barSizeSetting=f"{bar_minutes} mins",
                whatToShow="TRADES", useRTH=True, formatDate=2)
            candles = []
            for b in bars or []:
                ts = b.date if isinstance(b.date, datetime) else None
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                candles.append(Candle(ts=ts.astimezone(timezone.utc), open=float(b.open), high=float(b.high),
                                      low=float(b.low), close=float(b.close), volume=float(max(b.volume, 0))))
            return candles
        candles = self._call(run, timeout=60)
        return candles, self.snapshot().quotes

    def scan_candidates(self, n: int) -> list[tuple[str, str]]:
        """Candidatos del escáner de mercado de IBKR en el Nasdaq (más volumen y más subida del día). Solo lectura."""
        scans = (("HOT_BY_VOLUME", "Volumen inusual hoy en el Nasdaq (escáner de IBKR)"),
                 ("TOP_PERC_GAIN", "Entre las que más suben hoy en el Nasdaq (escáner de IBKR)"))

        async def run(ib):
            from ib_async import ScannerSubscription  # noqa: PLC0415

            out: list[tuple[str, str]] = []
            for code, why in scans:
                # Solo empresas conocidas y líquidas: capitalización > 2000 M$ (IB la expresa en millones),
                # precio > 10 $ y más de 1 millón de acciones negociadas hoy.
                sub = ScannerSubscription(instrument="STK", locationCode="STK.NASDAQ", scanCode=code,
                                          abovePrice=10, aboveVolume=1_000_000, marketCapAbove=2000,
                                          stockTypeFilter="CORP", numberOfRows=n)  # solo empresas, sin ETF
                for row in await ib.reqScannerDataAsync(sub) or []:
                    symbol = row.contractDetails.contract.symbol
                    if symbol and symbol not in {s for s, _ in out}:
                        out.append((symbol, why))
            return out
        return self._call(run, timeout=30)[:n]

    def current_positions(self) -> list[Position]:
        return list(self.snapshot().positions)

    async def _qualify(self, ib, symbol: str, currency: str):
        from ib_async import Stock

        contracts = await ib.qualifyContractsAsync(Stock(symbol, "SMART", currency))
        if not contracts or contracts[0] is None or not contracts[0].conId:
            raise UnknownSymbol(f"IBKR no reconoce la acción {symbol} en {currency}.")
        return contracts[0]

    # --- Bucle de conexión ---------------------------------------------------------------------------

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        from ib_async import IB
        from ib_async.ib import StartupFetch

        self._loop = asyncio.get_running_loop()
        fetch = StartupFetch.POSITIONS | StartupFetch.ACCOUNT_UPDATES
        if self.orders_enabled:
            fetch |= StartupFetch.ORDERS_OPEN | StartupFetch.ORDERS_COMPLETE | StartupFetch.EXECUTIONS
        attempt = 0
        while not self._stop.is_set():
            ib = IB()
            try:
                await ib.connectAsync(self.host, self.port, clientId=self.client_id,
                                      readonly=not self.orders_enabled, timeout=8, fetchFields=fetch)
                accounts = ib.managedAccounts()
                if not accounts:
                    raise ConnectionError("IB Gateway no ha indicado ninguna cuenta")
                if self.account_kind == "paper" and not all(is_paper_account(a) for a in accounts):
                    raise NotPaperAccountError(
                        "La sesión de IB Gateway no es de una cuenta Paper. Conexión cerrada: la app está configurada "
                        "para Paper. Inicia sesión en IB Gateway eligiendo «Negociación simulada».")
                if self.account_kind == "real" and any(is_paper_account(a) for a in accounts):
                    raise NotPaperAccountError(
                        "La app está configurada para tu cuenta real, pero IB Gateway tiene abierta una sesión Paper. "
                        "Inicia sesión con tu usuario real.")
                attempt = 0
                self._account = accounts[0]
                if self.orders_enabled:
                    self._watch_orders(ib)
                self._ib = ib
                log_event(log, "Conectado a IB Gateway", port=self.port, account=accounts[0],
                          kind=self.account_kind, orders=self.orders_enabled)
                await self._poll(ib, accounts[0])
            except NotPaperAccountError as exc:
                log_event(log, "Tipo de cuenta distinto del configurado: conexión rechazada", logging.ERROR)
                self._publish(BrokerSnapshot(link="desconectado", detail=str(exc)))
                ib.disconnect()
                return
            except Exception as exc:  # noqa: BLE001 - cualquier fallo de red se reintenta
                log_event(log, "Sin conexión con IB Gateway", logging.WARNING, error=str(exc), port=self.port)
                session = "de tu cuenta real" if self.account_kind == "real" else "Paper"
                detail = (f"No se puede conectar con IB Gateway en {self.host}:{self.port}. Comprueba que está "
                          f"abierto, con la sesión {session} iniciada y la API activada.")
                self._publish(BrokerSnapshot(link="reconectando" if attempt else "desconectado", detail=detail))
            finally:
                self._ib = None
                self._account = None
                if ib.isConnected():
                    ib.disconnect()
            delay = RECONNECT_SECONDS[min(attempt, len(RECONNECT_SECONDS) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    def _watch_orders(self, ib) -> None:
        def on_error(req_id, code, message, *_args):
            if req_id and req_id > 0:
                self._errors[req_id] = f"{code}: {message}"

        def on_trade(trade, *_args):
            self._sync_trade(trade)

        ib.errorEvent += on_error
        ib.orderStatusEvent += on_trade
        ib.execDetailsEvent += on_trade
        ib.commissionReportEvent += on_trade
        # Reconciliación al (re)conectar: lo que IBKR conoce manda; lo que no conoce queda en UNKNOWN.
        known = set()
        for trade in ib.trades():
            if trade.order.orderRef:
                known.add(trade.order.orderRef)
                self._sync_trade(trade)
        changed = mark_missing_as_unknown(self.orders, known, mode=self.order_mode)
        if changed:
            log_event(log, "Órdenes sin confirmar tras reconectar", logging.WARNING, orders=changed)

    def _sync_trade(self, trade) -> None:
        ref = trade.order.orderRef
        if not ref:
            return
        commission = None
        fees = [f.commissionReport for f in trade.fills if f.commissionReport and f.commissionReport.commission]
        if fees:
            commission = Money(amount=_q(sum((Decimal(str(r.commission)) for r in fees), Decimal("0"))),
                               currency=fees[0].currency or trade.contract.currency)
        st = trade.orderStatus
        try:
            sync_order(self.orders, BrokerOrderStatus(
                client_order_id=ref,
                status=st.status,
                filled=to_decimal(st.filled) or Decimal("0"),
                avg_fill_price=to_decimal(st.avgFillPrice) or None,
                commission=commission,
                order_id=trade.order.orderId or None,
                perm_id=trade.order.permId or None,
                message=self._errors.get(trade.order.orderId) or (trade.log[-1].message if trade.log else None),
            ))
        except Exception as exc:  # noqa: BLE001 - un fallo de sincronización no debe tumbar la conexión
            log_event(log, "No se pudo sincronizar la orden", logging.ERROR, client_order_id=ref, error=str(exc))

    async def _poll(self, ib, account_id: str) -> None:
        # 3 = diferidos si no hay suscripción; IB devuelve tiempo real cuando la cuenta la tiene.
        ib.reqMarketDataType(3)
        pnl = ib.reqPnL(account_id)
        tickers: dict[int, object] = {}
        while ib.isConnected() and not self._stop.is_set():
            items = [i for i in ib.portfolio(account_id) if i.position]
            for item in items:
                con = item.contract
                if con.conId not in tickers:
                    if not con.exchange:
                        con.exchange = "SMART"
                    tickers[con.conId] = ib.reqMktData(con, "", False, False)
            quotes = {cid: Quote(last=to_decimal(t.marketPrice()), close=to_decimal(t.close),
                                 freshness=_FRESHNESS_BY_TYPE.get(t.marketDataType, Freshness.UNAVAILABLE))
                      for cid, t in tickers.items()}
            freshness = overall_freshness(quotes.values())
            now = datetime.now(timezone.utc)
            if not self.orders_enabled:
                mode = "conexión de solo lectura"
            elif self.order_mode == "live":
                mode = "órdenes REALES con confirmación"
            else:
                mode = "conexión con envío de órdenes Paper"
            label = "cuenta REAL" if self.account_kind == "real" else "Paper"
            try:
                account = build_account(ib.accountValues(account_id), account_id=account_id,
                                        daily_pnl=to_decimal(pnl.dailyPnL), quotes=freshness, now=now,
                                        mode="paper" if is_paper_account(account_id) else "live")
            except ValueError as exc:
                self._publish(BrokerSnapshot(link="conectado", detail=str(exc), quotes=freshness, updated_at=now))
            else:
                self._publish(BrokerSnapshot(
                    link="conectado",
                    detail=f"IB Gateway {label} ({self.host}:{self.port}), {mode}",
                    account=account,
                    positions=build_positions(items, quotes),
                    quotes=freshness,
                    updated_at=now,
                ))
            await asyncio.sleep(REFRESH_SECONDS)
        raise ConnectionError("IB Gateway cerró la conexión")
