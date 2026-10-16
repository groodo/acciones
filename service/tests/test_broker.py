"""Conexión IBKR Paper: conversión de datos y garantías de solo lectura, sin IB Gateway real."""

import ast
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ib_async import AccountValue, Contract, PortfolioItem

import tradingdesk.broker as broker_module
from tradingdesk.api import create_app
from tradingdesk.broker import (
    BrokerSnapshot,
    Quote,
    build_account,
    build_positions,
    is_paper_account,
    mask_account,
    overall_freshness,
    to_decimal,
)
from tradingdesk.domain import Freshness, Money, Position
from tradingdesk.mock_data import MockProvider

from conftest import FIXED_NOW, TOKEN

ACC = "DU1234567"


def values(base="EUR"):
    rows = [("NetLiquidation", "1012.34", base), ("AvailableFunds", "600.10", base), ("BuyingPower", "2400.40", base),
            ("UnrealizedPnL", "2.85", base), ("RealizedPnL", "0", base), ("GrossPositionValue", "412.24", base),
            ("TotalCashValue", "600.10", base), ("CashBalance", "598.50", "EUR"), ("CashBalance", "1.60", "USD"),
            ("CashBalance", "600.10", "BASE"), ("ExchangeRate", "1", "EUR"), ("ExchangeRate", "0.92", "USD"),
            ("NetLiquidation", "999", "BASE")]
    out = [AccountValue(ACC, tag, val, cur, "") for tag, val, cur in rows]
    out.append(AccountValue("DU9999999", "NetLiquidation", "5", base, ""))  # otra cuenta: se ignora
    return out


def test_paper_accounts_only():
    assert is_paper_account("DU1234567") and is_paper_account("du1")
    assert not is_paper_account("U1234567")


def test_account_ids_are_masked():
    assert mask_account("DU1234567") == "DU****567"
    assert "1234" not in mask_account("U1234567")


@pytest.mark.parametrize("raw,expected", [(1.5, Decimal("1.5")), ("2.10", Decimal("2.10")), (float("nan"), None),
                                          (float("inf"), None), ("", None), (None, None), ("abc", None)])
def test_to_decimal(raw, expected):
    assert to_decimal(raw) == expected


def test_build_account_in_eur():
    snap = build_account(values(), account_id=ACC, daily_pnl=Decimal("12.34"), quotes=Freshness.DELAYED,
                         now=FIXED_NOW)
    assert snap.account_id_masked == "DU****567" and snap.mode == "paper"
    assert snap.net_liquidation_eur == Money(amount=Decimal("1012.34"), currency="EUR")
    assert snap.fx.pair == "EUR.USD" and snap.fx.rate == Decimal("1.0870")
    assert [m.currency for m in snap.settled_cash] == ["EUR", "USD"]
    assert snap.daily_pnl.amount == Decimal("12.34")
    assert snap.daily_pnl_pct == Decimal("1.23")  # 12,34 / 1000
    assert snap.exposure.amount == Decimal("412.24") and snap.invested_capital.amount == Decimal("409.39")
    assert snap.freshness is Freshness.DELAYED


def test_build_account_in_usd_converts_to_eur():
    rows = [v for v in values("USD") if v.tag != "ExchangeRate"]
    rows.append(AccountValue(ACC, "ExchangeRate", "1.085", "EUR", ""))
    snap = build_account(rows, account_id=ACC, daily_pnl=None, quotes=Freshness.REAL_TIME, now=FIXED_NOW)
    assert snap.net_liquidation.currency == "USD"
    assert snap.net_liquidation_eur == Money(amount=Decimal("933.03"), currency="EUR")
    assert "aún no" in snap.daily_pnl_basis


def test_build_account_without_net_liquidation_fails():
    with pytest.raises(ValueError):
        build_account([], account_id=ACC, daily_pnl=None, quotes=Freshness.UNAVAILABLE, now=FIXED_NOW)


def contract(con_id, symbol):
    return Contract(conId=con_id, symbol=symbol, secType="STK", currency="USD", localSymbol=symbol)


def test_build_positions_uses_quotes_and_skips_closed():
    items = [PortfolioItem(contract(1, "KO"), 3.0, 68.7, 206.1, 69.2033, -1.51, 0.0, ACC),
             PortfolioItem(contract(2, "AAPL"), 1.0, 231.0, 231.0, 228.4, 2.6, 0.0, ACC),
             PortfolioItem(contract(3, "MSFT"), 0.0, 400.0, 0.0, 0.0, 0.0, 5.0, ACC)]
    quotes = {2: Quote(last=Decimal("231.15"), close=Decimal("229.90"), freshness=Freshness.DELAYED)}
    pos = build_positions(items, quotes)
    assert [p.symbol for p in pos] == ["AAPL", "KO"]
    aapl, ko = pos
    assert aapl.last_price == Decimal("231.15") and aapl.change_pct == Decimal("0.54")
    assert ko.last_price == Decimal("68.70") and ko.change_pct is None  # sin cierre anterior no se inventa
    assert ko.avg_price == Decimal("69.20") and ko.unrealized_pnl.amount == Decimal("-1.51")


def test_overall_freshness():
    q = lambda f: Quote(None, None, f)  # noqa: E731
    assert overall_freshness([]) is Freshness.UNAVAILABLE
    assert overall_freshness([q(Freshness.REAL_TIME)]) is Freshness.REAL_TIME
    assert overall_freshness([q(Freshness.REAL_TIME), q(Freshness.DELAYED)]) is Freshness.DELAYED


def test_orders_only_go_out_through_submit_and_readonly_otherwise():
    source = Path(broker_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert not {c.func.attr for c in calls} & {"reqGlobalCancel", "bracketOrder", "oneCancelsAll", "whatIfOrder"}
    assert sum(c.func.attr == "placeOrder" for c in calls) == 1
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "IbkrPaperBroker")
    submit = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "submit")
    assert any(isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "placeOrder" for n in ast.walk(submit))
    assert "is_paper_account(self._account" in ast.get_source_segment(source, submit)
    connect = next(c for c in calls if c.func.attr == "connectAsync")
    readonly = next(k for k in connect.keywords if k.arg == "readonly")
    assert ast.unparse(readonly.value) == "not self.orders_enabled"


def test_submit_refuses_when_orders_disabled():
    b = broker_module.IbkrPaperBroker("127.0.0.1", 4002, 1)
    assert b.orders_enabled is False
    with pytest.raises(broker_module.BrokerUnavailable):
        b.submit("UI-00000001")
    with pytest.raises(broker_module.BrokerUnavailable):
        b.reference_price("AAPL", "USD")  # sin conexión


# --- API con un bróker falso ----------------------------------------------------------------------


@dataclass
class FakeBroker:
    snap: BrokerSnapshot
    started: bool = False
    stopped: bool = False
    calls: list = field(default_factory=list)
    orders_enabled: bool = False
    price: Decimal | None = Decimal("100")

    def reference_price(self, symbol, currency):
        self.calls.append(("price", symbol))
        return self.price

    def submit(self, client_order_id):
        self.calls.append(("submit", client_order_id))

    def cancel(self, client_order_id):
        self.calls.append(("cancel", client_order_id))

    def snapshot(self):
        return self.snap

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def connected_snapshot():
    acc = build_account(values(), account_id=ACC, daily_pnl=Decimal("1"), quotes=Freshness.DELAYED, now=FIXED_NOW)
    pos = [Position(symbol="KO", name="KO", quantity=Decimal("3"), currency="USD", avg_price=Decimal("69.20"),
                    last_price=Decimal("68.70"), unrealized_pnl=Money(amount=Decimal("-1.51"), currency="USD"),
                    change_pct=None)]
    return BrokerSnapshot(link="conectado", detail="IB Gateway Paper", account=acc, positions=pos,
                          quotes=Freshness.DELAYED, updated_at=FIXED_NOW)


def ibkr_client(settings, fake):
    app = create_app(settings, token=TOKEN, provider=MockProvider(now=FIXED_NOW), broker=fake)
    client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
    return app, client


def test_api_serves_ibkr_account_labelled_as_real_paper(settings):
    fake = FakeBroker(connected_snapshot())
    app, client = ibkr_client(settings, fake)
    with client as c:
        assert fake.started
        acc = c.get("/v1/account").json()
        assert (acc["source"], acc["is_mock"], acc["mode"]) == ("IBKR", False, "paper")
        assert "DIFERIDAS" in acc["notice"]
        assert acc["data"]["account_id_masked"] == "DU****567"
        pos = c.get("/v1/positions").json()
        assert pos["source"] == "IBKR" and pos["data"][0]["change_pct"] is None
        status = c.get("/v1/status").json()["data"]
        assert (status["broker"], status["mode_label"], status["quotes"]) == ("conectado", "PAPER", "diferidas")
        assert status["trading_allowed"] is False
        # Con IBKR nada se simula: ni señales, ni curva, ni órdenes de ejemplo.
        for path in ("/v1/signals", "/v1/equity-curve?range=7d", "/v1/orders", "/v1/kill-switch"):
            assert c.get(path).json()["is_mock"] is False, path
        assert c.get("/v1/orders").json()["data"] == []
        assert c.get("/v1/signals").json()["data"] == []
        curve = c.get("/v1/equity-curve?range=todo").json()["data"]
        assert [p["equity_eur"] for p in curve["points"]] == ["1012.34"] and curve["sufficient"] is False
        r = c.post("/v1/orders/simulated", json={"client_order_id": "UI-0000001", "symbol": "MSFT", "quantity": "1", "limit_price": "418"})
        assert r.status_code == 409
        ks = c.post("/v1/kill-switch", json={"active": True}).json()["data"]
        assert [p["symbol"] for p in ks["open_positions"]] == ["KO"]
    assert fake.stopped
    app.state.db.close()


def test_api_without_gateway_shows_no_balances(settings):
    fake = FakeBroker(BrokerSnapshot(link="reconectando", detail="No se puede conectar con IB Gateway"))
    app, client = ibkr_client(settings, fake)
    with client as c:
        for path in ("/v1/account", "/v1/positions"):
            r = c.get(path)
            assert r.status_code == 503 and "IB Gateway" in r.json()["detail"]
        status = c.get("/v1/status").json()["data"]
        assert status["broker"] == "reconectando" and status["alerts"][0].startswith("No se puede")
    app.state.db.close()


def test_real_account_broker_cannot_send_orders():
    b = broker_module.IbkrPaperBroker("127.0.0.1", 4001, 1, orders=object(), orders_enabled=True,
                                      account_kind="real")
    assert b.orders_enabled is False
    with pytest.raises(broker_module.BrokerUnavailable):
        b.submit("UI-00000001")
    with pytest.raises(broker_module.BrokerUnavailable):
        b.cancel("UI-00000001")


def test_api_with_real_account_is_labelled_and_refuses_orders(tmp_path):
    from tradingdesk.config import Settings  # noqa: PLC0415

    settings = Settings(data_dir=tmp_path, data_source="ibkr", ib_account="real-readonly", paper_orders=False,
                        ib_port=4001)
    real_values = [AccountValue("U7654321", v.tag, v.value, v.currency, "") for v in values() if v.account == ACC]
    acc = build_account(real_values, account_id="U7654321", daily_pnl=None, quotes=Freshness.DELAYED,
                        now=FIXED_NOW, mode="live")
    fake = FakeBroker(BrokerSnapshot(link="conectado", detail="IB Gateway cuenta REAL", account=acc,
                                     quotes=Freshness.DELAYED, updated_at=FIXED_NOW), orders_enabled=True)
    app, client = ibkr_client(settings, fake)
    with client as c:
        body = c.get("/v1/account").json()
        assert (body["mode"], body["data"]["mode"], body["data"]["account_id_masked"]) == ("live", "live", "U****321")
        status = c.get("/v1/status").json()["data"]
        assert (status["mode"], status["mode_label"]) == ("live", "REAL · SOLO LECTURA")
        assert "solo lectura" in status["alerts"][0]
        r = c.post("/v1/orders/paper", json={"client_order_id": "UI-00000001", "symbol": "AAPL", "side": "BUY",
                                              "quantity": "1", "limit_price": "100", "currency": "USD",
                                              "confirmed": True})
        assert r.status_code == 409
    assert not any(call[0] == "submit" for call in fake.calls)
    app.state.db.close()
