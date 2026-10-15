"""API HTTP local (solo localhost, autenticada con token).

No existe ningún endpoint que envíe órdenes a un bróker: en Hito A solo se
pueden crear borradores simulados y cancelarlos.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .db import Database
from .domain import (
    AccountSnapshot,
    EquityCurve,
    Envelope,
    Freshness,
    KillSwitchRequest,
    KillSwitchState,
    OrderEvent,
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
from .safety import KillSwitch, market_is_open

log = logging.getLogger("tradingdesk.api")

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}
CANCELLABLE = {OrderState.DRAFT, OrderState.RISK_APPROVED}


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
               db: Database | None = None) -> FastAPI:
    settings.validate()
    db = db or Database(settings.db_path)
    db.migrate()
    token = token or load_or_create_token(settings.token_path)
    provider = provider or MockProvider()
    orders = OrderStore(db)
    kill_switch = KillSwitch(db)
    seed_mock_orders(orders, provider.now())
    _record_snapshot(db, provider.account())

    app = FastAPI(title="AI Trading Desk — servicio local", version=__version__, docs_url=None, redoc_url=None)
    app.state.db = db

    def envelope(data, notice: str = MOCK_NOTICE) -> Envelope:
        return Envelope(source="MOCK", is_mock=True, mode=settings.mode,
                        generated_at=datetime.now(timezone.utc), notice=notice, data=data)

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
            market_detail="Sesión regular 9:30–16:00 America/New_York (festivos no contemplados)",
            quotes=Freshness.SIMULATED,
            last_update=now,
            kill_switch_active=ks,
            trading_allowed=False,
            alerts=alerts,
            service_version=__version__,
        ))

    @app.get("/v1/account", response_model=Envelope[AccountSnapshot])
    def account():
        return envelope(provider.account())

    @app.get("/v1/positions", response_model=Envelope[list[Position]])
    def positions():
        return envelope(provider.positions())

    @app.get("/v1/equity-curve", response_model=Envelope[EquityCurve])
    def equity_curve(range: Literal["hoy", "7d", "30d", "todo"] = "30d"):  # noqa: A002
        return envelope(provider.equity_curve(range))

    @app.get("/v1/signals", response_model=Envelope[list[Signal]])
    def signals():
        return envelope(provider.signals(),
                        notice=MOCK_NOTICE + " Las señales no proceden de ningún modelo.")

    @app.get("/v1/orders", response_model=Envelope[list[OrderRecord]])
    def list_orders(open_only: bool = False):
        return envelope(orders.list(mode=settings.mode, open_only=open_only))

    @app.get("/v1/orders/{client_order_id}/events", response_model=Envelope[list[OrderEvent]])
    def order_events(client_order_id: str):
        if orders.get(client_order_id) is None:
            raise HTTPException(404, "Orden no encontrada")
        return envelope(orders.events(client_order_id))

    @app.post("/v1/orders/simulated", response_model=Envelope[OrderRecord], status_code=201)
    def create_simulated(req: SimulatedOrderRequest):
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

    @app.post("/v1/orders/{client_order_id}/cancel", response_model=Envelope[OrderRecord])
    def cancel(client_order_id: str):
        order = orders.get(client_order_id)
        if order is None:
            raise HTTPException(404, "Orden no encontrada")
        if order.state not in CANCELLABLE:
            raise HTTPException(409, f"No se puede cancelar desde {order.state.value} en esta versión")
        return envelope(orders.transition(client_order_id, OrderState.CANCELLED, "Cancelada por el usuario"))

    def kill_switch_view() -> KillSwitchState:
        st = kill_switch.state()
        return KillSwitchState(
            active=st["active"],
            reason=st["reason"],
            changed_at=datetime.fromisoformat(st["changed_at"]) if st["changed_at"] else None,
            open_orders=orders.list(mode=settings.mode, open_only=True),
            open_positions=provider.positions(),
            message=("Operativa detenida. Se bloquean nuevas entradas; las órdenes y posiciones listadas "
                     "siguen activas y no se liquidan automáticamente.") if st["active"]
            else "Operativa habilitada en simulación.",
        )

    @app.get("/v1/kill-switch", response_model=Envelope[KillSwitchState])
    def get_kill_switch():
        return envelope(kill_switch_view())

    @app.post("/v1/kill-switch", response_model=Envelope[KillSwitchState])
    def set_kill_switch(req: KillSwitchRequest):
        kill_switch.set(req.active, req.reason)
        log_event(log, "Kill switch actualizado", active=req.active, reason=req.reason)
        return envelope(kill_switch_view())

    return app


def _record_snapshot(db: Database, snapshot: AccountSnapshot) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO account_snapshots (taken_at, mode, source, is_mock, payload) VALUES (?, ?, 'MOCK', 1, ?)",
            (snapshot.timestamp.isoformat(), snapshot.mode, snapshot.model_dump_json()),
        )
