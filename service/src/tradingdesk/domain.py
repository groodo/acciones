"""Contratos de datos (PRD §7).

Los importes usan ``Decimal`` y se serializan como texto en JSON para no perder
precisión. Todas las marcas de tiempo son UTC.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

DataSource = Literal["MOCK", "IBKR"]
Mode = Literal["paper", "live"]


class Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class Money(Model):
    amount: Decimal
    currency: str


class Freshness(str, Enum):
    REAL_TIME = "tiempo_real"
    DELAYED = "diferidas"
    UNAVAILABLE = "no_disponibles"
    SIMULATED = "simuladas"


class Envelope(Model, Generic[T]):
    """Toda respuesta indica de dónde salen los datos; los mocks nunca se ocultan."""

    source: DataSource
    is_mock: bool
    mode: Mode
    generated_at: datetime
    notice: str | None = None
    data: T


class FxRate(Model):
    pair: str
    rate: Decimal
    source: str
    as_of: datetime


class AccountSnapshot(Model):
    account_id_masked: str
    mode: Mode
    timestamp: datetime
    base_currency: str
    net_liquidation: Money
    net_liquidation_eur: Money
    fx: FxRate | None
    settled_cash: list[Money]
    available_funds: Money
    buying_power: Money
    unrealized_pnl: Money
    realized_pnl: Money
    realized_fees: Money
    daily_pnl: Money
    daily_pnl_pct: Decimal
    daily_pnl_basis: str
    invested_capital: Money
    exposure: Money
    risk_budget: Money
    freshness: Freshness


class Position(Model):
    symbol: str
    name: str
    quantity: Decimal
    currency: str
    avg_price: Decimal
    last_price: Decimal
    unrealized_pnl: Money
    change_pct: Decimal | None
    stop: Decimal | None = None
    target: Decimal | None = None
    estimated_fee: Money | None = None


class Proposal(str, Enum):
    BUY = "COMPRAR"
    SELL_POSITION = "VENDER_POSICION"
    WAIT = "ESPERAR"
    NOT_EVALUABLE = "NO_EVALUABLE"


class SignalStatus(str, Enum):
    STUDY = "EN_ESTUDIO"
    PAPER_READY = "APTA_PAPER"
    BLOCKED = "BLOQUEADA"
    EXPIRED = "CADUCADA"


class Signal(Model):
    """Fila de la tabla de Oportunidades. Sin porcentajes de «confianza»: no hay calibración."""

    symbol: str
    name: str
    price: Decimal | None
    change_pct: Decimal | None
    freshness: Freshness
    trend: str
    proposal: Proposal
    horizon_minutes: int
    net_eval_after_costs: Money | None
    status: SignalStatus
    reasons: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    model_version: str | None = None


class EquityPoint(Model):
    timestamp: datetime
    equity_eur: Decimal
    net_flows_eur: Decimal


class EquityCurve(Model):
    range: str
    sufficient: bool
    message: str | None
    points: list[EquityPoint]


class OrderState(str, Enum):
    DRAFT = "DRAFT"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderRecord(Model):
    client_order_id: str
    mode: Mode
    is_mock: bool
    symbol: str
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    currency: str
    state: OrderState
    created_at: datetime
    updated_at: datetime
    filled_quantity: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    commission: Money | None = None
    ibkr_order_id: int | None = None
    ibkr_perm_id: int | None = None
    note: str | None = None


class OrderEvent(Model):
    client_order_id: str
    from_state: OrderState | None
    to_state: OrderState
    reason: str
    at: datetime


class SimulatedOrderRequest(BaseModel):
    client_order_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    symbol: str = Field(min_length=1, max_length=12, pattern=r"^[A-Z.]+$")
    side: OrderSide = OrderSide.BUY
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=4)
    limit_price: Decimal = Field(gt=0, max_digits=12, decimal_places=4)
    currency: str = "USD"


class KillSwitchState(Model):
    active: bool
    reason: str | None
    changed_at: datetime | None
    open_orders: list[OrderRecord] = Field(default_factory=list)
    open_positions: list[Position] = Field(default_factory=list)
    message: str


class KillSwitchRequest(BaseModel):
    active: bool
    reason: str | None = Field(default=None, max_length=200)


class SystemStatus(Model):
    broker: Literal["conectado", "desconectado", "reconectando"]
    broker_detail: str
    mode: Mode
    mode_label: str
    market: Literal["abierto", "cerrado"]
    market_detail: str
    quotes: Freshness
    last_update: datetime
    kill_switch_active: bool
    trading_allowed: bool
    alerts: list[str]
    service_version: str
    max_order_value: Money | None = None
