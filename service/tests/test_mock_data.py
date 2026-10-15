from decimal import Decimal

from tradingdesk.domain import Freshness, Proposal
from tradingdesk.mock_data import MockProvider, usd_to_eur

from conftest import FIXED_NOW


def test_net_liquidation_equals_cash_plus_positions():
    p = MockProvider(now=FIXED_NOW)
    acc = p.account()
    cash = next(m.amount for m in acc.settled_cash if m.currency == "EUR")
    mv = sum((pos.quantity * pos.last_price for pos in p.positions()), Decimal("0"))
    assert abs(acc.net_liquidation_eur.amount - (cash + usd_to_eur(mv))) < Decimal("0.01")
    assert acc.unrealized_pnl.amount == acc.exposure.amount - acc.invested_capital.amount
    assert acc.freshness is Freshness.SIMULATED


def test_daily_pnl_matches_curve():
    p = MockProvider(now=FIXED_NOW)
    acc = p.account()
    pts = p.equity_curve("7d").points
    assert pts[-1].equity_eur - pts[-2].equity_eur == acc.daily_pnl.amount


def test_curve_ranges_are_consistent_and_today_is_insufficient():
    p = MockProvider(now=FIXED_NOW)
    assert p.equity_curve("hoy").sufficient is False
    assert p.equity_curve("hoy").points == []
    full = p.equity_curve("todo").points
    assert p.equity_curve("7d").points == full[-8:]
    assert p.equity_curve("30d").points == full[-31:]


def test_signals_never_invent_evaluations_without_data():
    for s in MockProvider(now=FIXED_NOW).signals():
        if s.proposal is Proposal.NOT_EVALUABLE:
            assert s.net_eval_after_costs is None and s.price is None
        assert s.model_version in (None, "mock-sin-modelo")
