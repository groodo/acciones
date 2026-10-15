from decimal import Decimal

import pytest

from tradingdesk.db import Database
from tradingdesk.domain import Money, OrderSide, OrderState as S
from tradingdesk.orders import ALLOWED_TRANSITIONS, TERMINAL_STATES, InvalidTransition, OrderError, OrderStore, TradingBlocked


def draft(store, coid="TEST-0001", qty="2", price="100.00"):
    return store.create_draft(client_order_id=coid, mode="paper", is_mock=True, symbol="AAPL",
                              side=OrderSide.BUY, quantity=Decimal(qty), limit_price=Decimal(price),
                              currency="USD")


def to_acknowledged(store, coid="TEST-0001"):
    for st in (S.RISK_APPROVED, S.SUBMITTING, S.ACKNOWLEDGED):
        store.transition(coid, st, "test")


def test_draft_is_idempotent(store):
    first, created = draft(store)
    again, created_again = draft(store)
    assert created and not created_again
    assert first == again
    assert len(store.list(mode="paper")) == 1


def test_reused_client_order_id_with_other_params_is_rejected(store):
    draft(store)
    with pytest.raises(OrderError):
        draft(store, qty="3")


def test_live_orders_cannot_be_created(store):
    with pytest.raises(TradingBlocked):
        store.create_draft(client_order_id="LIVE-0001", mode="live", is_mock=False, symbol="AAPL",
                           side=OrderSide.BUY, quantity=Decimal("1"), limit_price=Decimal("1"), currency="USD")


def test_terminal_states_have_no_exits():
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()


def test_unknown_is_never_resubmitted(store):
    draft(store)
    for st in (S.RISK_APPROVED, S.SUBMITTING, S.UNKNOWN):
        store.transition("TEST-0001", st, "pérdida de conexión tras SUBMITTING")
    assert S.SUBMITTING not in ALLOWED_TRANSITIONS[S.UNKNOWN]
    with pytest.raises(InvalidTransition):
        store.transition("TEST-0001", S.SUBMITTING, "reintento", via_reconciliation=True)
    with pytest.raises(InvalidTransition):
        store.transition("TEST-0001", S.ACKNOWLEDGED, "sin reconciliar")
    assert store.transition("TEST-0001", S.ACKNOWLEDGED, "reconciliado", via_reconciliation=True).state is S.ACKNOWLEDGED


def test_cannot_skip_risk_approval(store):
    draft(store)
    with pytest.raises(InvalidTransition):
        store.transition("TEST-0001", S.SUBMITTING, "saltando riesgo")


def test_partial_then_full_fill(store):
    draft(store)
    to_acknowledged(store)
    o = store.apply_fill("TEST-0001", Decimal("1"), Decimal("99.90"), None)
    assert o.state is S.PARTIALLY_FILLED and o.filled_quantity == Decimal("1")
    o = store.apply_fill("TEST-0001", Decimal("2"), Decimal("99.95"), Money(amount=Decimal("1.00"), currency="USD"))
    assert o.state is S.FILLED and o.commission.amount == Decimal("1.00")
    with pytest.raises(OrderError):
        store.apply_fill("TEST-0001", Decimal("3"), Decimal("99.95"), None)


def test_fill_cannot_go_backwards(store):
    draft(store)
    to_acknowledged(store)
    store.apply_fill("TEST-0001", Decimal("1"), Decimal("100"), None)
    with pytest.raises(OrderError):
        store.apply_fill("TEST-0001", Decimal("1"), Decimal("100"), None)


def test_events_are_audited_and_survive_restart(tmp_path):
    path = tmp_path / "orders.sqlite3"
    db = Database(path)
    db.migrate()
    store = OrderStore(db)
    draft(store)
    to_acknowledged(store)
    db.close()

    db = Database(path)
    store = OrderStore(db)
    events = store.events("TEST-0001")
    assert [e.to_state for e in events] == [S.DRAFT, S.RISK_APPROVED, S.SUBMITTING, S.ACKNOWLEDGED]
    assert store.get("TEST-0001").state is S.ACKNOWLEDGED
    assert db.query("SELECT COUNT(*) AS n FROM audit_log")[0]["n"] == 4
    db.close()


def test_amounts_are_stored_as_exact_text(store, db):
    draft(store, price="100.1234")
    raw = db.query("SELECT limit_price, typeof(limit_price) AS t FROM orders")[0]
    assert (raw["limit_price"], raw["t"]) == ("100.1234", "text")
