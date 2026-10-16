import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import tradingdesk.alpaca as alpaca_module
from tradingdesk.alpaca import AlpacaData, AlpacaError, HybridSource, load_keys, save_keys
from tradingdesk.domain import Freshness

NOW = datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc)  # 13:00 en Nueva York


def bar(ts, close=100.0):
    return {"t": ts.isoformat().replace("+00:00", "Z"), "o": close, "h": close + 1, "l": close - 1, "c": close, "v": 900}


def test_bars_keep_regular_session_closed_candles_and_follow_pages():
    pages = [
        {"bars": [bar(datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)),   # 8:00 NY: preapertura
                  bar(datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc))],  # 10:00 NY
         "next_page_token": "p2"},
        {"bars": [bar(NOW - timedelta(minutes=10)), bar(NOW - timedelta(minutes=3))],  # la última aún abierta
         "next_page_token": None},
    ]
    seen = []

    def get(url, headers):
        seen.append((url, headers))
        return pages.pop(0)

    candles = AlpacaData("KEYID123", "SECRET" * 4, get=get, clock=lambda: NOW).bars("AAPL", 5)
    assert [c.ts.hour for c in candles] == [14, 16]
    assert "feed=iex" in seen[0][0] and "page_token=p2" in seen[1][0]
    assert seen[0][1]["APCA-API-KEY-ID"] == "KEYID123"


def test_keys_file_is_private(tmp_path):
    path = tmp_path / "alpaca-keys.json"
    save_keys(path, "KEYID123", "S" * 20)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert load_keys(path) == ("KEYID123", "S" * 20)
    assert load_keys(tmp_path / "nada.json") is None


class Broker:
    def __init__(self):
        self.calls = []

    def historical_candles(self, symbol, bar_minutes):
        self.calls.append(symbol)
        return [], Freshness.DELAYED

    def current_positions(self):
        return []

    def scan_candidates(self, n):
        return [("AMD", "x")]


def test_hybrid_uses_alpaca_and_falls_back_to_ibkr():
    ok = AlpacaData("K" * 8, "S" * 16, get=lambda u, h: {"bars": []}, clock=lambda: NOW)
    src = HybridSource(Broker(), ok)
    assert src.historical_candles("AAPL", 5) == ([], Freshness.REAL_TIME)
    assert "IEX" in src.data_note and src.scan_candidates(3) == [("AMD", "x")]

    def fail(url, headers):
        raise AlpacaError("sin red")

    broker = Broker()
    src = HybridSource(broker, AlpacaData("K" * 8, "S" * 16, get=fail))
    assert src.historical_candles("AAPL", 5) == ([], Freshness.DELAYED) and broker.calls == ["AAPL"]
    assert "IBKR" in src.data_note


def test_api_stores_validated_keys_without_returning_the_secret(settings, monkeypatch):
    from tradingdesk.api import create_app
    from tradingdesk.mock_data import MockProvider

    from conftest import FIXED_NOW, TOKEN

    calls = []

    def fake_bars(self, symbol, bar_minutes, days=7):
        calls.append(symbol)
        if self._headers["APCA-API-KEY-ID"] == "BADKEY99":
            raise AlpacaError("Alpaca rechaza las claves de API (revisa que estén bien copiadas).")
        return []

    monkeypatch.setattr(alpaca_module.AlpacaData, "bars", fake_bars)
    app = create_app(settings, token=TOKEN, provider=MockProvider(now=FIXED_NOW))
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        assert c.get("/v1/data-provider").json()["configured"] is False
        bad = c.put("/v1/data-provider", json={"key_id": "BADKEY99", "secret_key": "s" * 20})
        assert bad.status_code == 422 and "claves" in bad.json()["detail"]
        assert not settings.alpaca_keys_path.exists()
        good = c.put("/v1/data-provider", json={"key_id": "PKABCDEF1234", "secret_key": "x" * 40}).json()
        assert good == {**good, "provider": "alpaca", "configured": True, "key_id_masked": "PK…1234"}
        assert "x" * 40 not in json.dumps(good)
        assert c.delete("/v1/data-provider").json()["configured"] is False
    app.state.db.close()


@pytest.mark.parametrize("key", ["con espacios", "a;b"])
def test_api_rejects_malformed_keys(settings, key):
    from tradingdesk.api import create_app
    from tradingdesk.mock_data import MockProvider

    from conftest import FIXED_NOW, TOKEN

    app = create_app(settings, token=TOKEN, provider=MockProvider(now=FIXED_NOW))
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        assert c.put("/v1/data-provider", json={"key_id": key, "secret_key": "x" * 20}).status_code == 422
    app.state.db.close()
