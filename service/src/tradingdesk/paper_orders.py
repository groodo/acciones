"""Órdenes en la cuenta Paper de IBKR: controles de riesgo y sincronización de estados.

Funciones puras (o que solo tocan ``OrderStore``) para poder probarlas sin IB Gateway.
El envío real vive en ``broker.IbkrPaperBroker``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, Field

from .domain import AccountSnapshot, Money, OrderSide, OrderState, Position
from .orders import ALLOWED_TRANSITIONS, OrderStore

S = OrderState
MAX_PRICE_DEVIATION = Decimal("0.05")  # el límite no puede alejarse más de un 5 % del precio de referencia


class PaperOrderRequest(BaseModel):
    client_order_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    symbol: str = Field(min_length=1, max_length=12, pattern=r"^[A-Z.]+$")
    side: OrderSide
    quantity: Decimal = Field(gt=0, max_digits=8, decimal_places=0)
    limit_price: Decimal = Field(gt=0, max_digits=12, decimal_places=4)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    # La app solo lo envía tras la confirmación explícita del usuario.
    confirmed: bool = False


class RiskRejection(Exception):
    """La orden no supera un control previo; no se crea ni se envía."""


def check_order_limits(req: PaperOrderRequest, *, positions: list[Position], max_order_value: Decimal) -> None:
    if not req.confirmed:
        raise RiskRejection("Falta tu confirmación explícita para enviar la orden.")
    value = req.quantity * req.limit_price
    if value > max_order_value:
        raise RiskRejection(f"Importe máximo de la orden: {value:.2f} {req.currency}. El tope por operación es "
                            f"{max_order_value:.2f} {req.currency}.")
    if req.side is OrderSide.SELL:
        held = sum((p.quantity for p in positions if p.symbol == req.symbol and p.currency == req.currency),
                   Decimal("0"))
        if req.quantity > held:
            raise RiskRejection(f"Solo puedes vender acciones que tengas: tienes {held:f} de {req.symbol}. "
                                "Las ventas en corto no están permitidas.")


def check_real_order_value(req: PaperOrderRequest, account: AccountSnapshot | None, cap_eur: Decimal) -> Decimal:
    """Tope por orden REAL en euros, con el tipo de cambio que da IBKR para la cuenta. Devuelve el importe en EUR."""
    value = req.quantity * req.limit_price
    if req.currency == "EUR":
        value_eur = value
    elif account is not None and account.fx is not None and account.fx.pair == f"EUR.{req.currency}" \
            and account.fx.rate > 0:
        value_eur = value / account.fx.rate
    else:
        raise RiskRejection(f"No hay tipo de cambio EUR/{req.currency} de IBKR: no se puede comprobar el tope en euros.")
    if value_eur > cap_eur:
        raise RiskRejection(f"La orden equivale a {value_eur:.2f} €. El tope por orden real es {cap_eur:.2f} €.")
    return value_eur


def check_price_band(req: PaperOrderRequest, reference: Decimal | None) -> None:
    if reference is None or reference <= 0:
        raise RiskRejection(f"No hay precio de referencia para {req.symbol} ahora mismo, así que no se puede "
                            "comprobar que el precio límite sea razonable.")
    deviation = abs(req.limit_price - reference) / reference
    if deviation > MAX_PRICE_DEVIATION:
        raise RiskRejection(f"El precio límite {req.limit_price} se aleja un {deviation * 100:.1f} % del precio de "
                            f"referencia ({reference}). El máximo permitido es un {MAX_PRICE_DEVIATION * 100:.0f} %.")


@dataclass(frozen=True)
class BrokerOrderStatus:
    """Lo que IBKR dice de una orden (``Trade.orderStatus`` + comisiones), sin depender de ib_async."""

    client_order_id: str
    status: str
    filled: Decimal
    avg_fill_price: Decimal | None
    commission: Money | None = None
    order_id: int | None = None
    perm_id: int | None = None
    message: str | None = None


ACK_STATUSES = {"PreSubmitted", "Submitted"}
CANCELLED_STATUSES = {"Cancelled", "ApiCancelled"}


def sync_order(store: OrderStore, st: BrokerOrderStatus) -> None:
    """Aplica a la orden local el estado que comunica IBKR. Idempotente: repetir el mismo estado no cambia nada."""
    order = store.get(st.client_order_id)
    if order is None or order.is_mock:
        return
    if st.order_id is not None or st.perm_id:
        store.set_broker_ids(order.client_order_id, st.order_id, st.perm_id or None)
    reconciling = order.state is S.UNKNOWN

    def move(target: OrderState, reason: str) -> None:
        nonlocal order
        if order.state is target or target not in ALLOWED_TRANSITIONS[order.state]:
            return
        order = store.transition(order.client_order_id, target, reason, via_reconciliation=order.state is S.UNKNOWN)

    detail = f" ({st.message})" if st.message else ""
    if st.status in ACK_STATUSES or st.filled > 0:
        move(S.ACKNOWLEDGED, "Aceptada por IBKR Paper" + (" (reconciliación)" if reconciling else ""))
    if st.filled > order.filled_quantity and st.avg_fill_price is not None:
        order = store.apply_fill(order.client_order_id, st.filled, st.avg_fill_price, st.commission)
    elif st.commission is not None and order.state is S.FILLED and order.commission != st.commission:
        store.set_commission(order.client_order_id, st.commission)
    if st.status in CANCELLED_STATUSES:
        if order.state is S.SUBMITTING:
            move(S.REJECTED, "IBKR no aceptó la orden" + detail)
        else:
            move(S.CANCELLED, "Cancelada en IBKR" + detail)
    elif st.status == "Inactive":
        if S.REJECTED in ALLOWED_TRANSITIONS[order.state]:
            move(S.REJECTED, "Rechazada por IBKR" + detail)
        else:
            move(S.CANCELLED, "Inactiva en IBKR" + detail)


def mark_missing_as_unknown(store: OrderStore, known_refs: set[str], mode: str = "paper") -> list[str]:
    """Tras conectar, las órdenes enviadas que IBKR no conoce pasan a UNKNOWN (nunca se reenvían)."""
    changed = []
    for order in store.list(mode=mode, is_mock=False, open_only=True):
        if order.client_order_id in known_refs:
            continue
        if order.state in (S.SUBMITTING, S.ACKNOWLEDGED, S.PARTIALLY_FILLED):
            store.transition(order.client_order_id, S.UNKNOWN,
                             "IBKR no la reconoce tras la reconexión; revisa la cuenta antes de repetirla")
            changed.append(order.client_order_id)
    return changed
