import pytest
from fastapi.testclient import TestClient

from tradingdesk.api import create_app, load_or_create_token
from tradingdesk.mock_data import MockProvider

from conftest import FIXED_NOW, TOKEN

READ_ENDPOINTS = ["/v1/status", "/v1/account", "/v1/positions", "/v1/equity-curve?range=7d",
                  "/v1/signals", "/v1/orders", "/v1/kill-switch"]


def test_health_needs_no_token(client):
    client.headers.pop("Authorization")
    assert client.get("/health").json()["status"] == "ok"


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_requires_token(client, path):
    assert client.get(path, headers={"Authorization": "Bearer otro"}).status_code == 401
    client.headers.pop("Authorization")
    assert client.get(path).status_code == 401


def test_rejects_browser_origin_and_foreign_host(client):
    assert client.get("/v1/status", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/v1/status", headers={"Host": "evil.example"}).status_code == 403


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_every_response_is_labelled_mock_paper(client, path):
    body = client.get(path).json()
    assert body["source"] == "MOCK" and body["is_mock"] is True and body["mode"] == "paper"
    assert "SIMULADOS" in body["notice"]


def test_status_never_allows_trading(client):
    data = client.get("/v1/status").json()["data"]
    assert data["trading_allowed"] is False
    assert data["mode_label"] == "SIMULACIÓN"
    assert data["broker"] == "desconectado"


def test_money_is_serialized_as_string(client):
    acc = client.get("/v1/account").json()["data"]
    assert isinstance(acc["net_liquidation_eur"]["amount"], str)
    assert acc["account_id_masked"].startswith("DU*")


def test_equity_curve_today_is_insufficient(client):
    data = client.get("/v1/equity-curve?range=hoy").json()["data"]
    assert data["sufficient"] is False and data["points"] == []
    assert client.get("/v1/equity-curve?range=1y").status_code == 422


def test_seeded_history_is_mock(client):
    orders = client.get("/v1/orders").json()["data"]
    assert len(orders) == 3 and all(o["is_mock"] for o in orders)
    events = client.get("/v1/orders/MOCK-AAPL-0001/events").json()["data"]
    assert [e["to_state"] for e in events][-1] == "FILLED"


ORDER = {"client_order_id": "UI-0000001", "symbol": "MSFT", "quantity": "1", "limit_price": "418.20"}


def test_simulated_order_is_idempotent(client):
    first = client.post("/v1/orders/simulated", json=ORDER)
    second = client.post("/v1/orders/simulated", json=ORDER)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["data"]["state"] == "DRAFT"
    assert client.post("/v1/orders/simulated", json={**ORDER, "quantity": "2"}).status_code == 409


def test_simulated_order_validation(client):
    assert client.post("/v1/orders/simulated", json={**ORDER, "quantity": "-1"}).status_code == 422
    assert client.post("/v1/orders/simulated", json={**ORDER, "symbol": "drop table"}).status_code == 422


def test_cancel_only_from_draft(client):
    client.post("/v1/orders/simulated", json=ORDER)
    assert client.post("/v1/orders/UI-0000001/cancel").json()["data"]["state"] == "CANCELLED"
    assert client.post("/v1/orders/UI-0000001/cancel").status_code == 409
    assert client.post("/v1/orders/MOCK-AAPL-0001/cancel").status_code == 409
    assert client.post("/v1/orders/NOPE-0000/cancel").status_code == 404


def test_kill_switch_blocks_new_entries_and_lists_what_remains(client):
    client.post("/v1/orders/simulated", json=ORDER)
    ks = client.post("/v1/kill-switch", json={"active": True, "reason": "prueba"}).json()["data"]
    assert ks["active"] is True
    assert [o["client_order_id"] for o in ks["open_orders"]] == ["UI-0000001"]
    assert len(ks["open_positions"]) == 2
    assert "no se liquidan" in ks["message"]
    blocked = client.post("/v1/orders/simulated", json={**ORDER, "client_order_id": "UI-0000002"})
    assert blocked.status_code == 423
    assert client.get("/v1/status").json()["data"]["kill_switch_active"] is True
    client.post("/v1/kill-switch", json={"active": False})
    assert client.post("/v1/orders/simulated", json={**ORDER, "client_order_id": "UI-0000002"}).status_code == 201


def test_no_route_can_submit_to_a_broker(client):
    paths = {r.path for r in client.app.routes}
    assert not any(word in p for p in paths for word in ("submit", "live", "real", "transmit", "place"))


def test_restart_keeps_state_and_does_not_reseed(settings):
    app = create_app(settings, token=TOKEN, provider=MockProvider(now=FIXED_NOW))
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        c.post("/v1/kill-switch", json={"active": True, "reason": "antes del reinicio"})
    app.state.db.close()
    app = create_app(settings, token=TOKEN, provider=MockProvider(now=FIXED_NOW))
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        assert c.get("/v1/kill-switch").json()["data"]["active"] is True
        assert len(c.get("/v1/orders").json()["data"]) == 3
    app.state.db.close()


def test_token_file_is_private_and_stable(tmp_path):
    path = tmp_path / "api-token"
    token = load_or_create_token(path)
    assert len(token) >= 32
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert load_or_create_token(path) == token
