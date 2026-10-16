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
from decimal import Decimal
from typing import Callable, Protocol

from .db import Database
from .domain import Freshness, Position, Proposal, Signal, SignalStatus
from .forecast import Candle, Evaluation, StrategyParams, SymbolInput, check_inputs, evaluate, not_evaluable
from .logging_setup import log_event
from .model_runner import Forecaster, ModelUnavailable

log = logging.getLogger("tradingdesk.signals")

MIN_CANDLES = 120
PRED_LEN = 12  # 12 velas de 5 min = 60 min
SAMPLES = 8
DISCOVERY_CANDIDATES = 15  # valores que pasan por Kronos en cada exploración (~5,5 s cada uno en un Mac M4)
DISCOVERY_EVERY = 3  # una exploración cada 3 ciclos (15 min con el intervalo por defecto)
DISCOVERY_SHOWN = 10

# Universo de reserva si el escáner de IBKR no responde: grandes valores del Nasdaq, se recorren por turnos.
NASDAQ_UNIVERSE = (
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA", "COST", "NFLX", "AMD", "PEP", "ADBE",
    "CSCO", "TMUS", "QCOM", "INTC", "INTU", "AMAT", "TXN", "ISRG", "BKNG", "HON", "AMGN", "SBUX", "MU",
    "LRCX", "ADI", "PANW", "KLAC", "MDLZ", "GILD", "MELI", "CRWD", "SNPS", "CDNS", "PYPL", "ABNB", "MRVL",
    "ORLY", "CTAS", "ASML", "FTNT", "WDAY", "DASH", "PLTR", "ARM", "SMCI", "COIN", "MSTR",
)


class CandleSource(Protocol):
    def historical_candles(self, symbol: str, bar_minutes: int) -> tuple[list[Candle], Freshness]: ...
    def current_positions(self) -> list[Position]: ...
    # Opcional: ``scan_candidates(n) -> list[tuple[str, str]]`` (símbolo, motivo) con el escáner de IBKR.


class SignalEngine:
    def __init__(self, db: Database, source: CandleSource, forecaster: Forecaster, watchlist: list[str],
                 params: StrategyParams | None = None, interval_s: int = 300,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.db, self.source, self.forecaster = db, source, forecaster
        self.watchlist = list(watchlist[:10])  # PRD §9: máximo diez símbolos en v1
        self.params = params or StrategyParams()
        self.interval_s = interval_s
        self.clock = clock
        self._signals: dict[str, Signal] = {}
        self._discovered: list[Signal] = []
        self.last_discovery: datetime | None = None
        self.discovery_detail = "La primera exploración del Nasdaq se hace tras el primer análisis de tu lista."
        self._cycle = 0
        self._universe_pos = 0
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

    def set_watchlist(self, symbols: list[str]) -> None:
        with self._lock:
            self.watchlist = list(symbols[:10])
            self._signals = {s: sig for s, sig in self._signals.items() if s in self.watchlist}

    def opportunities(self) -> list[Signal]:
        with self._lock:
            return list(self._discovered)

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
            # Hasta que IB Gateway conecta no hay velas: se espera en lugar de marcar todo «No evaluable»
            # durante un intervalo completo.
            ready = getattr(self.source, "ready", None)
            if ready is not None and not ready():
                self._stop.wait(5)
                continue
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
        for symbol in list(self.watchlist):
            ev = self._evaluate_symbol(symbol, positions.get(symbol))
            with self._lock:
                if symbol in self.watchlist:
                    self._signals[symbol] = ev.signal
        self.last_run = self.clock()
        if self._cycle % DISCOVERY_EVERY == 0:
            try:
                self.discover(positions)
            except Exception:  # noqa: BLE001 - la exploración nunca tumba el análisis de tu lista
                log.exception("Fallo en la exploración de oportunidades")
        self._cycle += 1
        return self.signals()

    def _candidates(self) -> list[tuple[str, str]]:
        scan = getattr(self.source, "scan_candidates", None)
        if scan is not None:
            try:
                found = [(s, why) for s, why in scan(DISCOVERY_CANDIDATES * 2) if s not in self.watchlist]
                if found:
                    self.discovery_detail = "Candidatos del escáner de mercado de IBKR (Nasdaq), analizados con Kronos."
                    return found[:DISCOVERY_CANDIDATES]
            except Exception as exc:  # noqa: BLE001
                log_event(log, "Escáner de IBKR no disponible", logging.WARNING, error=str(exc))
        # Reserva: se recorren por turnos los grandes valores del Nasdaq.
        pool = [s for s in NASDAQ_UNIVERSE if s not in self.watchlist]
        start = self._universe_pos % len(pool)
        chosen = (pool[start:] + pool[:start])[:DISCOVERY_CANDIDATES]
        self._universe_pos = start + DISCOVERY_CANDIDATES
        self.discovery_detail = ("El escáner de IBKR no respondió: se revisan por turnos los grandes valores del "
                                 "Nasdaq, 15 en cada exploración.")
        return [(s, "Gran valor del Nasdaq (revisión por turnos)") for s in chosen]

    def discover(self, positions: dict[str, Position] | None = None) -> list[Signal]:
        """Busca oportunidades fuera de tu lista: pasa los candidatos por Kronos y se queda con los mejores."""
        positions = positions or {}
        found: list[Signal] = []
        for symbol, why in self._candidates():
            if self._stop.is_set():
                break
            ev = self._evaluate_symbol(symbol, positions.get(symbol))
            sig = ev.signal
            if sig.proposal is Proposal.NOT_EVALUABLE:
                continue
            found.append(sig.model_copy(update={"reasons": [why, *sig.reasons]}))

        def rank(s: Signal):
            net = s.net_eval_after_costs.amount if s.net_eval_after_costs else Decimal("-1e9")
            return (s.proposal is not Proposal.BUY, -net)

        found.sort(key=rank)
        with self._lock:
            self._discovered = found[:DISCOVERY_SHOWN]
        self.last_discovery = self.clock()
        return self.opportunities()

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
        note = getattr(self.source, "data_note", None)
        if note:
            ev.signal = ev.signal.model_copy(update={"reasons": [*ev.signal.reasons, note]})
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
