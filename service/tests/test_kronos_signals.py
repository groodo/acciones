"""Hito C: previsiones de Kronos → señales, con un modelo falso (sin torch ni IBKR)."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tradingdesk.domain import Freshness, Money, Position, Proposal, SignalStatus
from tradingdesk.forecast import (Candle, ForecastResult, StrategyParams, SymbolInput, check_inputs, evaluate,
                                  horizon_stats)
from tradingdesk.model_runner import ModelUnavailable, ModelStatus, verify_lock
from tradingdesk.signal_engine import MIN_CANDLES, SignalEngine

NOW = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)  # 11:00 en Nueva York


def candles(n=150, start=100.0, step=0.0, end=NOW):
    first = end - timedelta(minutes=5 * n)
    out = []
    price = start
    for i in range(n):
        out.append(Candle(ts=first + timedelta(minutes=5 * i), open=price, high=price + 0.2, low=price - 0.2,
                          close=price + step, volume=1000))
        price += step
    return out


def result(paths):
    return ForecastResult(model_id="NeoQuasar/Kronos-small", model_version="kronos-test", paths=paths,
                          latency_ms=42, device="cpu")


def flat_paths(last, pct, n=8, spread=0.0):
    return [[last * (1 + (pct + spread * (i - n / 2) / n) / 100)] * 12 for i in range(n)]


def inp(cs, freshness=Freshness.REAL_TIME, position=None):
    return SymbolInput("MSFT", "MSFT", cs, freshness, NOW, position)


# --- Estadísticas y entradas -----------------------------------------------------------------------


def test_horizon_stats_are_empirical_quantiles():
    paths = [[100 + i] * 12 for i in range(11)]  # cierres 100..110
    stats = {s.minutes: s for s in horizon_stats(100.0, paths, 5)}
    assert set(stats) == {5, 15, 30, 60}
    assert stats[30].p50 == pytest.approx(5.0)
    assert stats[30].p10 == pytest.approx(1.0)
    assert stats[30].p90 == pytest.approx(9.0)


@pytest.mark.parametrize("cs,match", [
    (candles(10), "insuficientes"),
    (candles(150, end=NOW + timedelta(minutes=3)), "no ha cerrado"),
    (candles(150, end=NOW - timedelta(hours=2)), "antiguos"),
    ([Candle(NOW - timedelta(minutes=5 * (150 - i)), 1, 1, 1, 0, 1) for i in range(150)], "inválidos"),
])
def test_bad_inputs_are_not_evaluable(cs, match):
    assert match in check_inputs(inp(cs), StrategyParams(), MIN_CANDLES)


def test_good_inputs_pass():
    assert check_inputs(inp(candles()), StrategyParams(), MIN_CANDLES) is None


# --- Reglas de señal ------------------------------------------------------------------------------


def test_clear_positive_forecast_is_paper_ready_buy_with_net_after_costs():
    cs = candles(step=0.01)
    ev = evaluate(inp(cs), result(flat_paths(cs[-1].close, 2.0)), StrategyParams())
    assert ev.signal.proposal is Proposal.BUY
    assert ev.signal.status is SignalStatus.PAPER_READY
    assert ev.signal.net_eval_after_costs.amount > Decimal("0.50")
    assert ev.signal.model_version == "kronos-test"
    assert not any("%" in r and "confianza" in r.lower() for r in ev.signal.reasons)


def test_small_edge_that_does_not_cover_costs_waits():
    # +1 % sobre ~250 € no cubre 2 USD de comisiones más spread: lo correcto es no operar (PRD §9).
    cs = candles(step=0.01)
    ev = evaluate(inp(cs), result(flat_paths(cs[-1].close, 1.0)), StrategyParams())
    assert ev.signal.proposal is Proposal.WAIT


def test_delayed_quotes_block_execution():
    cs = candles(step=0.01)
    ev = evaluate(inp(cs, Freshness.DELAYED), result(flat_paths(cs[-1].close, 1.0)), StrategyParams())
    assert ev.signal.status is SignalStatus.BLOCKED


def test_disagreeing_samples_are_reported_and_do_not_buy():
    cs = candles(step=0.01)
    ev = evaluate(inp(cs), result(flat_paths(cs[-1].close, 1.0, spread=6.0)), StrategyParams())
    assert ev.signal.proposal is not Proposal.BUY
    assert ev.signal.contradictions


def test_negative_forecast_with_position_suggests_selling():
    cs = candles(step=-0.01)
    pos = Position(symbol="MSFT", name="MSFT", quantity=Decimal("1"), currency="USD", avg_price=Decimal("100"),
                   last_price=Decimal("98"), unrealized_pnl=Money(amount=Decimal("-2"), currency="USD"),
                   change_pct=None)
    ev = evaluate(inp(cs, position=pos), result(flat_paths(cs[-1].close, -1.0)), StrategyParams())
    assert ev.signal.proposal is Proposal.SELL_POSITION


def test_price_too_high_for_position_size_waits():
    cs = candles(start=900.0, step=0.01)
    ev = evaluate(inp(cs), result(flat_paths(cs[-1].close, 2.0)), StrategyParams())
    assert ev.signal.proposal is Proposal.WAIT


# --- Motor ------------------------------------------------------------------------------------------


class FakeSource:
    def __init__(self, cs, freshness=Freshness.REAL_TIME, fail=False):
        self.cs, self.freshness, self.fail = cs, freshness, fail

    def historical_candles(self, symbol, bar_minutes):
        if self.fail:
            raise RuntimeError("sin conexión")
        return self.cs, self.freshness

    def current_positions(self):
        return []


class FakeForecaster:
    def __init__(self, pct=1.0, fail=False):
        self.pct, self.fail, self.calls = pct, fail, []

    def status(self):
        return ModelStatus(True, True, "NeoQuasar/Kronos-small", "kronos-test", "cpu", "ok")

    def forecast(self, cs, pred_len, samples, bar_minutes):
        self.calls.append(cs)
        if self.fail:
            raise ModelUnavailable("Kronos no ha completado la predicción.")
        return result(flat_paths(cs[-1].close, self.pct, n=samples))


def test_engine_stores_prediction_and_never_sees_future_candles(db):
    cs = candles(step=0.01)
    fc = FakeForecaster(pct=2.0)
    engine = SignalEngine(db, FakeSource(cs), fc, ["MSFT"], clock=lambda: NOW)
    [sig] = engine.run_once()
    assert sig.proposal is Proposal.BUY
    assert all(c.ts + timedelta(minutes=5) <= NOW for c in fc.calls[0])
    row = db.query("SELECT * FROM predictions")[0]
    assert row["symbol"] == "MSFT" and row["model_version"] == "kronos-test" and row["calibrated"] == 0
    assert json.loads(row["payload"])["horizons"]


def test_model_failure_means_no_trade(db):
    engine = SignalEngine(db, FakeSource(candles()), FakeForecaster(fail=True), ["MSFT"], clock=lambda: NOW)
    [sig] = engine.run_once()
    assert sig.proposal is Proposal.NOT_EVALUABLE and sig.net_eval_after_costs is None
    assert "ninguna orden" in sig.reasons[0]
    assert db.query("SELECT COUNT(*) AS n FROM predictions")[0]["n"] == 0


def test_broker_failure_is_not_evaluable(db):
    engine = SignalEngine(db, FakeSource([], fail=True), FakeForecaster(), ["MSFT"], clock=lambda: NOW)
    [sig] = engine.run_once()
    assert sig.proposal is Proposal.NOT_EVALUABLE


def test_watchlist_capped_at_ten(db):
    engine = SignalEngine(db, FakeSource(candles()), FakeForecaster(), [f"S{i}" for i in range(15)])
    assert len(engine.watchlist) == 10


# --- Verificación de pesos --------------------------------------------------------------------------


def test_lock_verification(tmp_path):
    with pytest.raises(ModelUnavailable, match="no está instalado"):
        verify_lock(tmp_path)
    (tmp_path / "weights").mkdir()
    (tmp_path / "weights" / "m.bin").write_bytes(b"pesos")
    import hashlib
    good = hashlib.sha256(b"pesos").hexdigest()
    (tmp_path / "kronos.lock.json").write_text(json.dumps({"files": {"weights/m.bin": good}}))
    assert verify_lock(tmp_path)["files"]
    (tmp_path / "weights" / "m.bin").write_bytes(b"manipulado")
    with pytest.raises(ModelUnavailable, match="hash"):
        verify_lock(tmp_path)


# --- API --------------------------------------------------------------------------------------------


def test_api_serves_kronos_signals_labelled_as_ibkr(settings):
    from fastapi.testclient import TestClient

    from tradingdesk.api import create_app
    from tradingdesk.broker import BrokerSnapshot
    from tradingdesk.mock_data import MockProvider

    from conftest import FIXED_NOW, TOKEN

    class CandleBroker(FakeSource):
        orders_enabled = False

        def snapshot(self):
            return BrokerSnapshot(link="desconectado", detail="test")

        def start(self):
            pass

        def stop(self):
            pass

    app = create_app(settings, token=TOKEN, provider=MockProvider(now=FIXED_NOW),
                     broker=CandleBroker(candles(step=0.01)), forecaster=FakeForecaster())
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        body = c.get("/v1/signals").json()
        assert body["source"] == "IBKR" and body["is_mock"] is False
        assert "no son probabilidades" in body["notice"]
        assert c.get("/v1/model").json()["model_id"] == "NeoQuasar/Kronos-small"
    app.state.db.close()


class PerSymbolForecaster(FakeForecaster):
    """Previsión distinta por símbolo: el engine evalúa los símbolos en orden."""

    def __init__(self, pcts):
        super().__init__()
        self.pcts = list(pcts)

    def forecast(self, cs, pred_len, samples, bar_minutes):
        self.calls.append(cs)
        return result(flat_paths(cs[-1].close, self.pcts.pop(0) if self.pcts else 0.0, n=samples))


class ScanSource(FakeSource):
    def __init__(self, cs, found):
        super().__init__(cs)
        self.found = found

    def scan_candidates(self, n):
        return self.found


def test_discovery_uses_ibkr_scanner_and_ranks_buys_first(db):
    found = [("MSFT", "en tu lista"), ("AMD", "volumen"), ("PLTR", "sube")]
    # MSFT (tu lista) +0 %, luego AMD +0 % (esperar) y PLTR +2 % (comprar)
    engine = SignalEngine(db, ScanSource(candles(step=0.01), found), PerSymbolForecaster([0.0, 0.0, 2.0]),
                          ["MSFT"], clock=lambda: NOW)
    engine.run_once()
    opps = engine.opportunities()
    assert [s.symbol for s in opps] == ["PLTR", "AMD"]  # sin MSFT, que ya está en tu lista
    assert opps[0].proposal is Proposal.BUY and opps[0].reasons[0] == "sube"
    assert "escáner" in engine.discovery_detail and engine.last_discovery == NOW


def test_discovery_falls_back_to_nasdaq_universe_in_turns(db):
    from tradingdesk.signal_engine import DISCOVERY_CANDIDATES, NASDAQ_UNIVERSE

    engine = SignalEngine(db, FakeSource(candles(step=0.01)), FakeForecaster(pct=0.0), ["AAPL"], clock=lambda: NOW)
    first = {s.symbol for s in engine.discover()}
    second = {s.symbol for s in engine.discover()}
    assert "AAPL" not in first | second and not first & second
    assert len(first) <= DISCOVERY_CANDIDATES and first <= set(NASDAQ_UNIVERSE)
    assert "por turnos" in engine.discovery_detail


def test_api_watchlist_and_order_limit_are_configurable(settings):
    from fastapi.testclient import TestClient

    from tradingdesk.api import create_app
    from tradingdesk.mock_data import MockProvider

    from conftest import FIXED_NOW, TOKEN

    app = create_app(settings, token=TOKEN, provider=MockProvider(now=FIXED_NOW))
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        assert c.put("/v1/watchlist", json={"symbols": ["tsla", "AMD", "AMD"]}).json()["symbols"] == ["TSLA", "AMD"]
        assert c.get("/v1/watchlist").json()["symbols"] == ["TSLA", "AMD"]
        assert c.put("/v1/watchlist", json={"symbols": []}).status_code == 422
        assert c.put("/v1/watchlist", json={"symbols": ["NO VALE"]}).status_code == 422
        assert c.put("/v1/order-limit", json={"max_real_order_eur": "250"}).json()["max_real_order_eur"] == "250.00"
        assert c.get("/v1/order-limit").json()["max_real_order_eur"] == "250.00"
        assert c.put("/v1/order-limit", json={"max_real_order_eur": "1500"}).status_code == 422
    app.state.db.close()
    # Se conserva tras reiniciar.
    app = create_app(settings, token=TOKEN, provider=MockProvider(now=FIXED_NOW))
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        assert c.get("/v1/watchlist").json()["symbols"] == ["TSLA", "AMD"]
        assert c.get("/v1/order-limit").json()["max_real_order_eur"] == "250.00"
    app.state.db.close()
