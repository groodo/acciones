"""Órdenes Paper: controles de riesgo, sincronización con IBKR y API, con un bróker falso."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tradingdesk.api import create_app
from tradingdesk.config import Settings
from tradingdesk.domain import Money, OrderSide, OrderState, Position
from tradingdesk.mock_data import MockProvider
from tradingdesk.paper_orders import (
    BrokerOrderStatus,
    PaperOrderRequest,
    RiskRejection,
    check_order_limits,
    check_price_band,
    mark_missing_as_unknown,
    sync_order,
)

from conftest import FIXED_NOW, TOKEN
from test_broker import FakeBroker, connected_snapshot

S = OrderState
KO = Position(symbol="KO", name="KO", quantity=Decimal("3"), currency="USD", avg_price=Decimal("69.20"),
              last_price=Decimal("68.70"), unrealized_pnl=Money(amount=Decimal("-1.51"), currency="USD"),
              change_pct=None)


def req(**kw):
    base = dict(client_order_id="UI-PAPER-0001", symbol="KO", side="BUY", quantity="2", limit_price="68.70",
                confirmed=True)
    return PaperOrderRequest(**{**base, **kw})


# --- Controles de riesgo --------------------------------------------------------------------------


def test_limits_accept_a_reasonable_order():
    check_order_limits(req(), positions=[KO], max_order_value=Decimal("1000"))
    check_price_band(req(), Decimal("68.50"))


@pytest.mark.parametrize("kwargs,match", [
    ({"confirmed": False}, "confirmación"),
    ({"quantity": "20"}, "tope"),
    ({"side": "SELL", "quantity": "4"}, "corto"),
    ({"side": "SELL", "symbol": "AAPL", "quantity": "1"}, "corto"),
])
def test_limits_reject(kwargs, match):
    with pytest.raises(RiskRejection, match=match):
        check_order_limits(req(**kwargs), positions=[KO], max_order_value=Decimal("1000"))


def test_selling_what_you_hold_is_allowed():
    check_order_limits(req(side="SELL", quantity="3"), positions=[KO], max_order_value=Decimal("1000"))


@pytest.mark.parametrize("reference,ok", [(Decimal("68.70"), True), (Decimal("65.50"), True),
                                          (Decimal("64"), False), (Decimal("80"), False), (None, False)])
def test_price_band(reference, ok):
    if ok:
        check_price_band(req(), reference)
    else:
        with pytest.raises(RiskRejection):
            check_price_band(req(), reference)


def test_request_rejects_fractional_or_bad_values():
    with pytest.raises(ValueError):
        req(quantity="1.5")
    with pytest.raises(ValueError):
        req(currency="usd")


# --- Sincronización con los estados de IBKR ------------------------------------------------------


def submitted(store, coid="UI-PAPER-0001", qty="2"):
    store.create_draft(client_order_id=coid, mode="paper", is_mock=False, symbol="KO", side=OrderSide.BUY,
                       quantity=Decimal(qty), limit_price=Decimal("68.70"), currency="USD")
    store.transition(coid, S.RISK_APPROVED, "ok")
    store.transition(coid, S.SUBMITTING, "envío")
    return coid


def status(status, filled="0", avg=None, **kw):
    return BrokerOrderStatus(client_order_id="UI-PAPER-0001", status=status, filled=Decimal(filled),
                             avg_fill_price=Decimal(avg) if avg else None, **kw)


def test_ack_partial_fill_and_commission(store):
    submitted(store)
    sync_order(store, status("Submitted", order_id=7, perm_id=123))
    sync_order(store, status("Submitted"))  # repetido: idempotente
    o = store.get("UI-PAPER-0001")
    assert (o.state, o.ibkr_order_id, o.ibkr_perm_id) == (S.ACKNOWLEDGED, 7, 123)
    sync_order(store, status("Submitted", "1", "68.69"))
    assert store.get("UI-PAPER-0001").state is S.PARTIALLY_FILLED
    sync_order(store, status("Filled", "2", "68.695"))
    sync_order(store, status("Filled", "2", "68.695", commission=Money(amount=Decimal("1.00"), currency="USD")))
    o = store.get("UI-PAPER-0001")
    assert o.state is S.FILLED and o.filled_quantity == 2 and o.commission.amount == Decimal("1.00")
    assert [e.to_state for e in store.events("UI-PAPER-0001")][-3:] == [S.ACKNOWLEDGED, S.PARTIALLY_FILLED,
                                                                         S.FILLED]


def test_immediate_fill_without_ack_goes_through_acknowledged(store):
    submitted(store)
    sync_order(store, status("Filled", "2", "68.70"))
    assert [e.to_state for e in store.events("UI-PAPER-0001")][-2:] == [S.ACKNOWLEDGED, S.FILLED]


def test_cancel_before_ack_is_a_rejection_with_reason(store):
    submitted(store)
    sync_order(store, status("Cancelled", message="321: API en modo solo lectura"))
    o = store.get("UI-PAPER-0001")
    assert o.state is S.REJECTED
    assert "solo lectura" in store.events("UI-PAPER-0001")[-1].reason


def test_cancel_after_ack(store):
    submitted(store)
    sync_order(store, status("Submitted"))
    sync_order(store, status("Cancelled"))
    assert store.get("UI-PAPER-0001").state is S.CANCELLED


def test_mock_orders_are_never_touched(store):
    store.create_draft(client_order_id="MOCK-X-0001", mode="paper", is_mock=True, symbol="KO",
                       side=OrderSide.BUY, quantity=Decimal("1"), limit_price=Decimal("1"), currency="USD")
    sync_order(store, BrokerOrderStatus("MOCK-X-0001", "Filled", Decimal("1"), Decimal("1")))
    assert store.get("MOCK-X-0001").state is S.DRAFT


def test_reconnect_marks_unknown_and_reconciles_later(store):
    submitted(store)
    submitted(store, "UI-PAPER-0002")
    assert mark_missing_as_unknown(store, known_refs={"UI-PAPER-0002"}) == ["UI-PAPER-0001"]
    assert store.get("UI-PAPER-0001").state is S.UNKNOWN
    sync_order(store, status("Filled", "2", "68.70"))  # IBKR la reconoce después: reconciliación
    assert store.get("UI-PAPER-0001").state is S.FILLED


# --- API -----------------------------------------------------------------------------------------


class SubmittingBroker(FakeBroker):
    """Simula IB Gateway: al enviar, la orden pasa a SUBMITTING y luego IBKR la acepta."""

    store = None

    def submit(self, client_order_id):
        super().submit(client_order_id)
        self.store.transition(client_order_id, S.SUBMITTING, "Enviando a IBKR Paper")
        sync_order(self.store, BrokerOrderStatus(client_order_id, "Submitted", Decimal("0"), None, order_id=9))


@pytest.fixture
def paper(settings):
    snap = connected_snapshot()
    fake = SubmittingBroker(snap, orders_enabled=True, price=Decimal("68.70"))
    app = create_app(settings, token=TOKEN, provider=MockProvider(now=FIXED_NOW), broker=fake)
    from tradingdesk.orders import OrderStore
    fake.store = OrderStore(app.state.db)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        yield c, fake
    app.state.db.close()


BODY = {"client_order_id": "UI-PAPER-0001", "symbol": "KO", "side": "BUY", "quantity": "2",
        "limit_price": "68.70", "confirmed": True}


def test_paper_order_end_to_end(paper):
    c, fake = paper
    status_data = c.get("/v1/status").json()["data"]
    assert status_data["trading_allowed"] is True and status_data["max_order_value"]["amount"] == "1000"
    r = c.post("/v1/orders/paper", json=BODY)
    assert r.status_code == 201
    body = r.json()
    assert (body["source"], body["is_mock"]) == ("IBKR", False)
    assert body["data"]["state"] == "ACKNOWLEDGED" and body["data"]["ibkr_order_id"] == 9
    states = [e["to_state"] for e in c.get("/v1/orders/UI-PAPER-0001/events").json()["data"]]
    assert states == ["DRAFT", "RISK_APPROVED", "SUBMITTING", "ACKNOWLEDGED"]
    # Repetir la petición no reenvía.
    assert c.post("/v1/orders/paper", json=BODY).status_code == 200
    assert [x for x in fake.calls if x[0] == "submit"] == [("submit", "UI-PAPER-0001")]
    # Cancelar una orden aceptada se pide a IBKR.
    assert c.post("/v1/orders/UI-PAPER-0001/cancel").status_code == 202
    assert ("cancel", "UI-PAPER-0001") in fake.calls


@pytest.mark.parametrize("change,code", [
    ({"confirmed": False}, 422),
    ({"quantity": "100"}, 422),
    ({"limit_price": "80"}, 422),
    ({"side": "SELL", "quantity": "5"}, 422),
    ({"quantity": "0.5"}, 422),
])
def test_paper_order_rejections_send_nothing(paper, change, code):
    c, fake = paper
    assert c.post("/v1/orders/paper", json={**BODY, **change}).status_code == code
    assert not [x for x in fake.calls if x[0] == "submit"]
    assert c.get("/v1/orders/UI-PAPER-0001/events").status_code == 404


def test_kill_switch_blocks_paper_orders(paper):
    c, fake = paper
    c.post("/v1/kill-switch", json={"active": True})
    assert c.get("/v1/status").json()["data"]["trading_allowed"] is False
    assert c.post("/v1/orders/paper", json=BODY).status_code == 423
    assert not fake.calls


def test_paper_orders_need_ibkr(client):
    assert client.post("/v1/orders/paper", json=BODY).status_code == 409


def test_paper_orders_disabled_in_config(settings):
    fake = FakeBroker(connected_snapshot(), orders_enabled=False)
    app = create_app(settings, token=TOKEN, provider=MockProvider(now=FIXED_NOW), broker=fake)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        assert c.post("/v1/orders/paper", json=BODY).status_code == 409
        assert c.get("/v1/status").json()["data"]["trading_allowed"] is False
    app.state.db.close()


def test_settings_for_paper_orders(tmp_path):
    s = Settings.from_env({"TRADINGDESK_DATA_DIR": str(tmp_path), "TRADINGDESK_MAX_ORDER_VALUE": "250",
                           "TRADINGDESK_PAPER_ORDERS": "0"})
    assert (s.max_order_value, s.paper_orders) == (Decimal("250"), False)
