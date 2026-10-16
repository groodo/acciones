"""Bucle de análisis: velas de IBKR → Kronos → señal (PRD §6 SignalEngine).

No tiene acceso a funciones de envío de órdenes: solo lee velas y posiciones del
bróker y escribe previsiones. Si Kronos o los datos fallan, la señal queda en
``No evaluable`` (nunca se sustituye por una previsión antigua o inventada).
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Protocol

from .db import Database
from .domain import Freshness, Position, Signal, SignalStatus
from .forecast import Candle, Evaluation, StrategyParams, SymbolInput, check_inputs, evaluate, not_evaluable
from .logging_setup import log_event
from .model_runner import Forecaster, ModelUnavailable

log = logging.getLogger("tradingdesk.signals")

MIN_CANDLES = 120
PRED_LEN = 12  # 12 velas de 5 min = 60 min
SAMPLES = 8


class CandleSource(Protocol):
    def historical_candles(self, symbol: str, bar_minutes: int) -> tuple[list[Candle], Freshness]: ...
    def current_positions(self) -> list[Position]: ...


class SignalEngine:
    def __init__(self, db: Database, source: CandleSource, forecaster: Forecaster, watchlist: list[str],
                 params: StrategyParams | None = None, interval_s: int = 300,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.db, self.source, self.forecaster = db, source, forecaster
        self.watchlist = watchlist[:10]  # PRD §9: máximo diez símbolos en v1
        self.params = params or StrategyParams()
        self.interval_s = interval_s
        self.clock = clock
        self._signals: dict[str, Signal] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run: datetime | None = None

    def signals(self) -> list[Signal]:
        with self._lock:
            if not self._signals:
                return [Signal(symbol=s, name=s, price=None, change_pct=None, freshness=Freshness.UNAVAILABLE,
                               trend="—", proposal="NO_EVALUABLE", horizon_minutes=self.params.signal_horizon_min,
                               net_eval_after_costs=None, status=SignalStatus.STUDY,
                               reasons=["Análisis pendiente: aún no se ha ejecutado el primer ciclo."])
                        for s in self.watchlist]
            return [self._signals[s] for s in self.watchlist if s in self._signals]

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="signal-engine", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - el bucle no debe morir
                log.exception("Fallo en el ciclo de señales")
            self._stop.wait(self.interval_s)

    def run_once(self) -> list[Signal]:
        try:
            positions = {p.symbol: p for p in self.source.current_positions()}
        except Exception:  # noqa: BLE001
            positions = {}
        for symbol in self.watchlist:
            ev = self._evaluate_symbol(symbol, positions.get(symbol))
            with self._lock:
                self._signals[symbol] = ev.signal
        self.last_run = self.clock()
        return self.signals()

    def _evaluate_symbol(self, symbol: str, position: Position | None) -> Evaluation:
        now = self.clock()
        try:
            candles, freshness = self.source.historical_candles(symbol, self.params.bar_minutes)
            inp = SymbolInput(symbol, symbol, candles, freshness, now, position)
        except Exception as exc:  # noqa: BLE001
            inp = SymbolInput(symbol, symbol, [], Freshness.UNAVAILABLE, now, position,
                              error=f"No se pudieron obtener velas de IBKR: {exc}")
        problem = check_inputs(inp, self.params, MIN_CANDLES)
        if problem:
            return not_evaluable(inp, problem)
        try:
            result = self.forecaster.forecast(inp.candles, PRED_LEN, SAMPLES, self.params.bar_minutes)
        except ModelUnavailable as exc:
            return not_evaluable(inp, f"{exc} No se propondrá ninguna orden.")
        ev = evaluate(inp, result, self.params)
        self._store(inp, result, ev)
        log_event(log, "Señal evaluada", symbol=symbol, proposal=ev.signal.proposal.value,
                  status=ev.signal.status.value, latency_ms=result.latency_ms)
        return ev

    def _store(self, inp: SymbolInput, result, ev: Evaluation) -> None:
        """IA-01: se guarda entrada, modelo, hora, horizonte, versión, resultado y condiciones de validez."""
        payload = {
            "horizons": [h.__dict__ for h in ev.horizons],
            "paths": result.paths,
            "latency_ms": result.latency_ms,
            "device": result.device,
            "last_close": inp.candles[-1].close,
            "freshness": inp.freshness.value,
            "proposal": ev.signal.proposal.value,
            "status": ev.signal.status.value,
            "reasons": ev.signal.reasons,
            "valid_until": (inp.now.timestamp() + 600),
        }
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO predictions (id, model_id, model_version, symbol, input_cutoff, candles,
                       horizon_minutes, payload, calibrated, data_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                (uuid.uuid4().hex, result.model_id, result.model_version, inp.symbol,
                 inp.candles[-1].ts.isoformat(), len(inp.candles), PRED_LEN * self.params.bar_minutes,
                 json.dumps(payload), ev.data_id or "", inp.now.isoformat()),
            )
