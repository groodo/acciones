"""Ventas automáticas de ganancia y pérdida: orden bracket al comprar y protección de posiciones."""

import asyncio
from decimal import Decimal

import pytest

import tradingdesk.broker as broker_module
from tradingdesk.db import MIGRATIONS, Database
from tradingdesk.domain import Money, OrderSide, OrderState, Position
from tradingdesk.orders import OrderStore
from tradingdesk.paper_orders import RiskRejection, check_protection_prices, free_shares, protection_ids

from test_broker import REAL_ORDER, FakeBroker, _real_settings, _real_snapshot, ibkr_client

S = OrderState


def test_protection_prices_must_bracket_the_entry():
    check_protection_prices(Decimal("100"), Decimal("102"), Decimal("99"))
    for tp, sl in (("100", "99"), ("102", "100"), ("151", "99"), ("102", "49"), (None, "99")):
        with pytest.raises(RiskRejection):
            check_protection_prices(Decimal("100"), tp and Decimal(tp), Decimal(sl))


def test_protection_ids_fit_the_id_rules():
    tp, sl = protection_ids("X" * 64)
    assert (len(tp), tp[-3:], sl[-3:]) == (63, "-TP", "-SL")


def test_migration_adds_order_type_to_existing_databases(tmp_path):
    db = Database(tmp_path / "old.sqlite3")
    db.migrate(MIGRATIONS[:1])
    db.query("INSERT INTO orders (client_order_id, mode, is_mock, symbol, side, quantity, limit_price, currency, "
             "state, created_at, updated_at) VALUES ('OLD-0001', 'live', 0, 'KO', 'BUY', '1', '60', 'USD', 'FILLED', "
             "'2026-10-16T10:00:00+00:00', '2026-10-16T10:00:00+00:00')")
    assert db.migrate() == 2
    old = OrderStore(db).get("OLD-0001")
    assert (old.order_type, old.parent_client_order_id) == ("LMT", None)
    db.close()


def _position(symbol="AAPL", qty="3"):
    return Position(symbol=symbol, name=symbol, quantity=Decimal(qty), currency="USD", avg_price=Decimal("95"),
                    last_price=Decimal("100"), unrealized_pnl=Money(amount=Decimal("15"), currency="USD"),
                    change_pct=None)


def test_real_buy_with_protection_sends_bracket(tmp_path):
    fake = FakeBroker(_real_snapshot(), orders_enabled=True)
    fake.order_mode = "live"
    app, client = ibkr_client(_real_settings(tmp_path), fake)
    order = {**REAL_ORDER, "take_profit_price": "102", "stop_loss_price": "99"}
    with client as c:
        assert c.post("/v1/orders/real", json={**order, "stop_loss_price": "101"}).status_code == 422
        assert c.post("/v1/orders/real", json={**order, "side": "SELL"}).status_code == 422
        r = c.post("/v1/orders/real", json=order)
        assert r.status_code == 201
        rows = {o["client_order_id"]: o for o in c.get("/v1/orders").json()["data"]}
        tp, sl = rows["UI-00000009-TP"], rows["UI-00000009-SL"]
        assert (tp["side"], tp["order_type"], tp["limit_price"], tp["parent_client_order_id"]) == \
            ("SELL", "LMT", "102", "UI-00000009")
        assert (sl["order_type"], sl["limit_price"], sl["mode"]) == ("STP", "99", "live")
        assert all(o["state"] == "RISK_APPROVED" for o in rows.values())  # el falso no los mueve
        # Repetir la misma petición no reenvía nada.
        assert c.post("/v1/orders/real", json=order).status_code == 200
    assert [call for call in fake.calls if call[0] in ("submit", "protect")] == \
        [("protect", "UI-00000009", "UI-00000009-TP", "UI-00000009-SL")]
    app.state.db.close()


def test_protect_existing_position_only_with_free_shares(tmp_path):
    snap = _real_snapshot()
    fake = FakeBroker(snap.__class__(**{**snap.__dict__, "positions": [_position()]}), orders_enabled=True)
    fake.order_mode = "live"
    app, client = ibkr_client(_real_settings(tmp_path), fake)
    req = {"client_order_id": "PR-00000001", "symbol": "AAPL", "quantity": "3", "take_profit_price": "103",
           "stop_loss_price": "97", "confirmed": True}
    with client as c:
        assert c.post("/v1/orders/protect", json={**req, "confirmed": False}).status_code == 422
        assert c.post("/v1/orders/protect", json={**req, "quantity": "4"}).status_code == 422
        assert c.post("/v1/orders/protect", json={**req, "take_profit_price": "99"}).status_code == 422
        r = c.post("/v1/orders/protect", json=req)
        assert r.status_code == 201
        assert [o["order_type"] for o in r.json()["data"]] == ["LMT", "STP"]
        # Las 3 acciones ya están comprometidas: no se pueden proteger dos veces.
        again = c.post("/v1/orders/protect", json={**req, "client_order_id": "PR-00000002", "quantity": "1"})
        assert again.status_code == 422 and "0" in again.json()["detail"]
        # La posición muestra su stop y su objetivo.
        pos = c.get("/v1/positions").json()["data"][0]
        assert (pos["stop"], pos["target"]) == ("97", "103")
        # El kill switch frena entradas, no protecciones.
        c.post("/v1/kill-switch", json={"active": True})
        assert c.post("/v1/orders/protect", json=req).status_code == 200
    assert [call for call in fake.calls if call[0] == "protect"] == [("protect", None, "PR-00000001-TP", "PR-00000001-SL")]
    app.state.db.close()


def test_protect_refused_in_read_only_account(tmp_path):
    fake = FakeBroker(_real_snapshot(), orders_enabled=True)
    fake.order_mode = "live"
    app, client = ibkr_client(_real_settings(tmp_path, "real-readonly"), fake)
    with client as c:
        r = c.post("/v1/orders/protect", json={"client_order_id": "PR-00000001", "symbol": "AAPL", "quantity": "1",
                                               "take_profit_price": "103", "stop_loss_price": "97", "confirmed": True})
        assert r.status_code == 409
    assert fake.calls == [] or all(call[0] != "protect" for call in fake.calls)
    app.state.db.close()


def test_free_shares_ignores_children_of_unfilled_buys(store):
    from tradingdesk.domain import OrderRecord  # noqa: PLC0415

    def rec(cid, side, qty, parent=None):
        return OrderRecord(client_order_id=cid, mode="live", is_mock=False, symbol="AAPL", side=side,
                           quantity=Decimal(qty), limit_price=Decimal("1"), currency="USD", state=S.ACKNOWLEDGED,
                           created_at="2026-10-16T10:00:00Z", updated_at="2026-10-16T10:00:00Z",
                           parent_client_order_id=parent)
    sells = [rec("A-TP", OrderSide.SELL, "2"), rec("A-SL", OrderSide.SELL, "2"), rec("B", OrderSide.SELL, "1")]
    assert free_shares("AAPL", "USD", [_position(qty="5")], sells) == Decimal("2")


class _FakeIBClient:
    def __init__(self):
        self.next_id = 100

    def getReqId(self):
        self.next_id += 1
        return self.next_id


class _FakeIB:
    def __init__(self):
        self.client = _FakeIBClient()
        self.placed = []

    def placeOrder(self, contract, order):
        self.placed.append(order)
        order.permId = 0

        class Trade:
            pass
        trade = Trade()
        trade.order = order
        return trade


def _live_broker(store):
    b = broker_module.IbkrPaperBroker("127.0.0.1", 4001, 1, orders=store, orders_enabled=True,
                                      account_kind="real", allow_real_orders=True)
    b._account = "U7654321"
    ib = _FakeIB()

    async def qualify(_ib, symbol, currency):
        return object()
    b._qualify = qualify
    b._call = lambda factory, timeout=15: asyncio.run(factory(ib))
    return b, ib


def _approved(store, cid, side, price, order_type="LMT", parent=None):
    store.create_draft(client_order_id=cid, mode="live", is_mock=False, symbol="AAPL", side=side,
                       quantity=Decimal("2"), limit_price=Decimal(price), currency="USD", allow_live=True,
                       order_type=order_type, parent_client_order_id=parent)
    store.transition(cid, S.RISK_APPROVED, "test")


def test_broker_bracket_links_children_to_parent_and_transmits_once(store):
    _approved(store, "UI-00000001", OrderSide.BUY, "100")
    _approved(store, "UI-00000001-TP", OrderSide.SELL, "102", parent="UI-00000001")
    _approved(store, "UI-00000001-SL", OrderSide.SELL, "99", "STP", parent="UI-00000001")
    b, ib = _live_broker(store)
    b.submit_protected("UI-00000001-TP", "UI-00000001-SL", "UI-00000001")
    parent, tp, sl = ib.placed
    assert (parent.action, parent.orderType, parent.lmtPrice, parent.transmit, parent.tif) == ("BUY", "LMT", 100, False, "DAY")
    assert (tp.action, tp.orderType, tp.lmtPrice, tp.parentId, tp.transmit, tp.tif) == ("SELL", "LMT", 102, parent.orderId, False, "GTC")
    assert (sl.action, sl.orderType, sl.auxPrice, sl.parentId, sl.transmit, sl.tif) == ("SELL", "STP", 99, parent.orderId, True, "GTC")
    assert {o.orderRef for o in ib.placed} == {"UI-00000001", "UI-00000001-TP", "UI-00000001-SL"}
    assert all(store.get(i).state is S.SUBMITTING for i in ("UI-00000001", "UI-00000001-TP", "UI-00000001-SL"))


def test_broker_protection_uses_one_cancels_all(store):
    _approved(store, "PR-00000001-TP", OrderSide.SELL, "103")
    _approved(store, "PR-00000001-SL", OrderSide.SELL, "97", "STP")
    b, ib = _live_broker(store)
    b.submit_protected("PR-00000001-TP", "PR-00000001-SL")
    tp, sl = ib.placed
    assert tp.ocaGroup == sl.ocaGroup == "OCA-PR-00000001-TP" and tp.ocaType == sl.ocaType == 1
    assert (tp.parentId, tp.transmit, sl.transmit) == (0, False, True)
