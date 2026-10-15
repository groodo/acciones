"""Proveedor de datos SIMULADOS para Hito A.

Nada de lo que hay aquí procede de IBKR ni de Kronos. Los valores son
deterministas para que la UI y los tests sean reproducibles, y todas las
respuestas que los usan van etiquetadas con ``source="MOCK"`` e ``is_mock=True``.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from .domain import (
    AccountSnapshot,
    EquityCurve,
    EquityPoint,
    Freshness,
    FxRate,
    Money,
    OrderSide,
    OrderState,
    Position,
    Proposal,
    Signal,
    SignalStatus,
)
from .orders import OrderStore

CENT = Decimal("0.01")
MOCK_NOTICE = "DATOS SIMULADOS: no proceden de IBKR ni de Kronos. Ninguna cifra refleja una cuenta real."
MOCK_MODEL_VERSION = "mock-sin-modelo"
MOCK_FX = Decimal("1.0850")  # USD por EUR, valor ficticio

_POSITIONS = [
    # símbolo, nombre, cantidad, precio medio USD, último USD, cierre anterior USD
    ("AAPL", "Apple Inc.", Decimal("1"), Decimal("228.40"), Decimal("231.15"), Decimal("229.90")),
    ("KO", "Coca-Cola Co.", Decimal("3"), Decimal("69.20"), Decimal("68.75"), Decimal("69.05")),
]
_SETTLED_CASH_EUR = Decimal("598.50")
_REALIZED_EUR = Decimal("3.20")
_REALIZED_FEES_EUR = Decimal("2.00")
_RISK_PER_TRADE_EUR = Decimal("2.50")
_DEPOSIT_EUR = Decimal("1000.00")


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def usd_to_eur(amount_usd: Decimal) -> Decimal:
    return amount_usd / MOCK_FX


class MockProvider:
    def __init__(self, now: datetime | None = None):
        self._fixed_now = now

    def now(self) -> datetime:
        return self._fixed_now or datetime.now(timezone.utc)

    # --- Posiciones y cuenta -------------------------------------------------

    def positions(self) -> list[Position]:
        result = []
        for symbol, name, qty, avg, last, prev in _POSITIONS:
            result.append(
                Position(
                    symbol=symbol,
                    name=name,
                    quantity=qty,
                    currency="USD",
                    avg_price=avg,
                    last_price=last,
                    unrealized_pnl=Money(amount=_q((last - avg) * qty), currency="USD"),
                    change_pct=_q((last - prev) / prev * 100),
                    stop=_q(avg * Decimal("0.98")),
                    target=_q(avg * Decimal("1.03")),
                    estimated_fee=Money(amount=Decimal("1.00"), currency="USD"),
                )
            )
        return result

    def _market_value_usd(self, field: int) -> Decimal:
        return sum((row[2] * row[field] for row in _POSITIONS), Decimal("0"))

    def net_liquidation_eur(self) -> Decimal:
        return _q(_SETTLED_CASH_EUR + usd_to_eur(self._market_value_usd(4)))

    def previous_close_eur(self) -> Decimal:
        return _q(_SETTLED_CASH_EUR + usd_to_eur(self._market_value_usd(5)))

    def account(self) -> AccountSnapshot:
        now = self.now()
        nlv = self.net_liquidation_eur()
        prev = self.previous_close_eur()
        daily = nlv - prev
        cost_eur = _q(usd_to_eur(self._market_value_usd(3)))
        exposure_eur = _q(usd_to_eur(self._market_value_usd(4)))
        unrealized_eur = exposure_eur - cost_eur
        eur = lambda v: Money(amount=_q(v), currency="EUR")  # noqa: E731
        return AccountSnapshot(
            account_id_masked="DU*****123",
            mode="paper",
            timestamp=now,
            base_currency="EUR",
            net_liquidation=eur(nlv),
            net_liquidation_eur=eur(nlv),
            fx=FxRate(pair="EUR.USD", rate=MOCK_FX, source="MOCK (tasa ficticia)", as_of=now),
            settled_cash=[eur(_SETTLED_CASH_EUR), Money(amount=Decimal("0.00"), currency="USD")],
            available_funds=eur(_SETTLED_CASH_EUR),
            buying_power=eur(_SETTLED_CASH_EUR),
            unrealized_pnl=eur(unrealized_eur),
            realized_pnl=eur(_REALIZED_EUR),
            realized_fees=eur(_REALIZED_FEES_EUR),
            daily_pnl=eur(daily),
            daily_pnl_pct=_q(daily / prev * 100),
            daily_pnl_basis="(Valor actual − valor al cierre anterior) / valor al cierre anterior. Sin aportaciones ni retiradas hoy.",
            invested_capital=eur(cost_eur),
            exposure=eur(exposure_eur),
            risk_budget=eur(_RISK_PER_TRADE_EUR * len(_POSITIONS)),
            freshness=Freshness.SIMULATED,
        )

    # --- Curva de patrimonio -------------------------------------------------

    def equity_curve(self, range_: str) -> EquityCurve:
        if range_ == "hoy":
            return EquityCurve(
                range=range_,
                sufficient=False,
                message="Datos insuficientes: aún no hay histórico intradía.",
                points=[],
            )
        days = {"7d": 7, "30d": 30, "todo": 45}[range_]
        rng = random.Random(42)
        today = self.now().replace(hour=20, minute=0, second=0, microsecond=0)
        # Se genera la serie completa (45 días) hacia atrás desde el cierre anterior
        # y se recorta, para que todos los rangos sean coherentes entre sí.
        values = [self.previous_close_eur()]
        for _ in range(44):
            values.append(_q(values[-1] - Decimal(rng.randint(-450, 450)) / 100))
        values.reverse()
        points = [
            EquityPoint(
                timestamp=today - timedelta(days=45 - i),
                equity_eur=v,
                net_flows_eur=_DEPOSIT_EUR if i == 0 else Decimal("0.00"),
            )
            for i, v in enumerate(values)
        ]
        points.append(EquityPoint(timestamp=self.now(), equity_eur=self.net_liquidation_eur(),
                                  net_flows_eur=Decimal("0.00")))
        return EquityCurve(range=range_, sufficient=True, message=None, points=points[-(days + 1):])

    # --- Señales -------------------------------------------------------------

    def signals(self) -> list[Signal]:
        eur = lambda v: Money(amount=Decimal(v), currency="EUR")  # noqa: E731
        return [
            Signal(
                symbol="MSFT", name="Microsoft Corp.", price=Decimal("418.20"), change_pct=Decimal("0.64"),
                freshness=Freshness.SIMULATED, trend="Alcista suave", proposal=Proposal.BUY, horizon_minutes=30,
                net_eval_after_costs=eur("0.85"), status=SignalStatus.PAPER_READY,
                reasons=["Escenario simulado con cierre por encima de la media de 20 velas",
                         "Spread simulado de 0,02 USD"],
                contradictions=["Volumen por debajo de la media de la sesión"],
                model_version=MOCK_MODEL_VERSION,
            ),
            Signal(
                symbol="AAPL", name="Apple Inc.", price=Decimal("231.15"), change_pct=Decimal("0.54"),
                freshness=Freshness.SIMULATED, trend="Lateral", proposal=Proposal.WAIT, horizon_minutes=15,
                net_eval_after_costs=eur("-0.40"), status=SignalStatus.STUDY,
                reasons=["El beneficio esperado no cubre comisiones y spread"],
                model_version=MOCK_MODEL_VERSION,
            ),
            Signal(
                symbol="KO", name="Coca-Cola Co.", price=Decimal("68.75"), change_pct=Decimal("-0.43"),
                freshness=Freshness.SIMULATED, trend="Bajista suave", proposal=Proposal.SELL_POSITION,
                horizon_minutes=60, net_eval_after_costs=eur("0.30"), status=SignalStatus.STUDY,
                reasons=["Precio simulado cerca del stop de protección"],
                model_version=MOCK_MODEL_VERSION,
            ),
            Signal(
                symbol="NVDA", name="NVIDIA Corp.", price=None, change_pct=None,
                freshness=Freshness.DELAYED, trend="—", proposal=Proposal.NOT_EVALUABLE, horizon_minutes=5,
                net_eval_after_costs=None, status=SignalStatus.BLOCKED,
                reasons=["Sin cotizaciones en tiempo real: análisis pausado"],
                model_version=None,
            ),
            Signal(
                symbol="SPY", name="SPDR S&P 500 ETF", price=Decimal("571.30"), change_pct=Decimal("0.12"),
                freshness=Freshness.SIMULATED, trend="Lateral", proposal=Proposal.WAIT, horizon_minutes=30,
                net_eval_after_costs=eur("0.05"), status=SignalStatus.EXPIRED,
                reasons=["La propuesta caducó: pasaron más de 10 minutos desde los datos de entrada"],
                model_version=MOCK_MODEL_VERSION,
            ),
        ]


def seed_mock_orders(store: OrderStore, now: datetime) -> None:
    """Rellena un historial de órdenes simuladas la primera vez (idempotente)."""
    if store.list(mode="paper", is_mock=True):
        return
    day = timedelta(days=1)
    history = [
        ("MOCK-AAPL-0001", "AAPL", Decimal("1"), Decimal("228.40"), now - 3 * day, OrderState.FILLED),
        ("MOCK-KO-0001", "KO", Decimal("3"), Decimal("69.20"), now - 2 * day, OrderState.FILLED),
        ("MOCK-MSFT-0001", "MSFT", Decimal("1"), Decimal("410.00"), now - day, OrderState.CANCELLED),
    ]
    for coid, symbol, qty, price, at, final in history:
        store.create_draft(client_order_id=coid, mode="paper", is_mock=True, symbol=symbol, side=OrderSide.BUY,
                           quantity=qty, limit_price=price, currency="USD",
                           note="Orden SIMULADA de ejemplo", created_at=at)
        if final is OrderState.CANCELLED:
            store.transition(coid, OrderState.CANCELLED, "Descartada por el usuario (simulación)", at=at)
            continue
        steps = [(OrderState.RISK_APPROVED, "Aprobada por riesgo (simulación)"),
                 (OrderState.SUBMITTING, "Envío simulado"),
                 (OrderState.ACKNOWLEDGED, "Acuse simulado")]
        for i, (state, reason) in enumerate(steps, start=1):
            store.transition(coid, state, reason, at=at + timedelta(seconds=i))
        store.apply_fill(coid, qty, price, Money(amount=Decimal("1.00"), currency="USD"),
                         at=at + timedelta(seconds=5))
