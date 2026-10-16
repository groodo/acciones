"""De previsiones de Kronos a señales (PRD §8 y §9). Lógica pura, sin torch ni IBKR.

Kronos genera trayectorias posibles de velas. Aquí se resumen en cuantiles
empíricos de rentabilidad por horizonte y se convierten en una propuesta
determinista. Las frecuencias de las muestras NO son probabilidades calibradas:
nunca se muestran como «% de confianza».
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .domain import Freshness, Money, Position, Proposal, Signal, SignalStatus

HORIZONS_MIN = (5, 15, 30, 60)
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Candle:
    """Vela OHLCV cerrada. ``ts`` es la hora de apertura en UTC."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class ForecastResult:
    """Salida del modelo: ``paths[i][k]`` es el cierre previsto de la vela k+1 en la muestra i."""

    model_id: str
    model_version: str
    paths: list[list[float]]
    latency_ms: int
    device: str


@dataclass(frozen=True)
class HorizonStats:
    minutes: int
    p10: float
    p50: float
    p90: float


@dataclass(frozen=True)
class StrategyParams:
    """Valores por defecto de investigación del PRD §9 (configurables, no garantías)."""

    bar_minutes: int = 5
    max_position_eur: Decimal = Decimal("250")
    fx_usd_per_eur: Decimal = Decimal("1.08")  # solo para dimensionar; el FX real lo da IBKR
    commission_usd_per_side: Decimal = Decimal("1.00")
    spread_slippage_bps: Decimal = Decimal("10")  # ida y vuelta, conservador
    min_net_edge_eur: Decimal = Decimal("0.50")
    max_downside_pct: float = 1.0  # p10 del horizonte no puede ser peor que -1 %
    signal_horizon_min: int = 30
    max_data_age: timedelta = timedelta(minutes=15)


@dataclass(frozen=True)
class SymbolInput:
    symbol: str
    name: str
    candles: list[Candle]
    freshness: Freshness
    now: datetime
    position: Position | None = None
    error: str | None = None


@dataclass
class Evaluation:
    signal: Signal
    horizons: list[HorizonStats] = field(default_factory=list)
    data_id: str | None = None


def data_fingerprint(candles: list[Candle]) -> str:
    raw = json.dumps([[c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume] for c in candles])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        raise ValueError("sin valores")
    pos = (len(sorted_values) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def horizon_stats(last_close: float, paths: list[list[float]], bar_minutes: int) -> list[HorizonStats]:
    """Cuantiles empíricos de la rentabilidad (%) del cierre a cada horizonte."""
    stats = []
    for minutes in HORIZONS_MIN:
        k = minutes // bar_minutes
        if k < 1 or any(len(p) < k for p in paths):
            continue
        returns = sorted((p[k - 1] / last_close - 1) * 100 for p in paths)
        stats.append(HorizonStats(minutes, _quantile(returns, 0.1), _quantile(returns, 0.5), _quantile(returns, 0.9)))
    return stats


def _q(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def not_evaluable(inp: SymbolInput, reason: str, status: SignalStatus = SignalStatus.BLOCKED,
                  model_version: str | None = None) -> Evaluation:
    last = inp.candles[-1].close if inp.candles else None
    return Evaluation(Signal(
        symbol=inp.symbol, name=inp.name, price=_q(Decimal(str(last))) if last else None, change_pct=None,
        freshness=inp.freshness, trend="—", proposal=Proposal.NOT_EVALUABLE,
        horizon_minutes=StrategyParams.signal_horizon_min, net_eval_after_costs=None, status=status,
        reasons=[reason], model_version=model_version,
    ))


def check_inputs(inp: SymbolInput, params: StrategyParams, min_candles: int) -> str | None:
    """Motivo por el que no se puede evaluar, o ``None`` si los datos son aptos."""
    if inp.error:
        return inp.error
    if len(inp.candles) < min_candles:
        return f"Datos insuficientes: {len(inp.candles)} velas de {min_candles} necesarias."
    if any(c.close <= 0 or c.high < c.low for c in inp.candles):
        return "Velas con precios inválidos: no se evalúa."
    last_end = inp.candles[-1].ts + timedelta(minutes=params.bar_minutes)
    if last_end > inp.now:
        return "La última vela aún no ha cerrado: se evita usar datos incompletos."
    if inp.now - last_end > params.max_data_age:
        return "Datos antiguos (mercado cerrado o sin actualizar): no se evalúa."
    return None


def evaluate(inp: SymbolInput, forecast: ForecastResult, params: StrategyParams) -> Evaluation:
    """Convierte una previsión en señal. Sin «% de confianza»; el neto descuenta costes estimados."""
    last_close = inp.candles[-1].close
    stats = horizon_stats(last_close, forecast.paths, params.bar_minutes)
    by_h = {s.minutes: s for s in stats}
    h = by_h.get(params.signal_horizon_min)
    data_id = data_fingerprint(inp.candles)
    if h is None:
        ev = not_evaluable(inp, "La previsión no cubre el horizonte de la señal.", model_version=forecast.model_version)
        ev.data_id = data_id
        return ev

    price = Decimal(str(last_close))
    max_usd = params.max_position_eur * params.fx_usd_per_eur
    qty = int(max_usd // price) if price > 0 else 0
    notional_usd = price * qty
    costs_usd = 2 * params.commission_usd_per_side + notional_usd * params.spread_slippage_bps / Decimal("10000")
    gross_usd = notional_usd * Decimal(str(h.p50)) / 100
    net_eur = _q((gross_usd - costs_usd) / params.fx_usd_per_eur) if qty else None

    first = inp.candles[-(params.signal_horizon_min // params.bar_minutes) - 1].close if len(inp.candles) > 6 else last_close
    change = _q(Decimal(str((last_close / inp.candles[0].close - 1) * 100)))
    trend = "Alcista" if h.p50 > 0.05 else ("Bajista" if h.p50 < -0.05 else "Lateral")
    reasons = [f"Mediana prevista a {h.minutes} min: {h.p50:+.2f} % (rango de muestras p10 {h.p10:+.2f} % / p90 {h.p90:+.2f} %)."]
    contradictions: list[str] = []
    if (last_close - first) * h.p50 < 0:
        contradictions.append("La previsión va en sentido contrario al movimiento reciente.")
    if h.p10 < 0 < h.p90:
        contradictions.append("Las muestras del modelo no coinciden en la dirección.")

    blocked = inp.freshness is not Freshness.REAL_TIME
    if blocked:
        reasons.append("Cotizaciones no en tiempo real: la señal no puede ejecutarse (IA-02).")

    held = inp.position is not None and inp.position.quantity > 0
    if qty == 0:
        proposal, status = Proposal.WAIT, SignalStatus.STUDY
        reasons.append("Con el tamaño máximo por posición no se llega a una acción.")
    elif held and h.p50 < 0 and h.p90 < 0.1:
        proposal, status = Proposal.SELL_POSITION, SignalStatus.STUDY
        reasons.append("Tienes posición y la previsión es negativa.")
    elif net_eur is not None and net_eur >= params.min_net_edge_eur and h.p10 > -params.max_downside_pct \
            and not contradictions:
        proposal, status = Proposal.BUY, SignalStatus.PAPER_READY
        reasons.append(f"Neto estimado tras comisiones y spread: {net_eur} € para {qty} acciones.")
    else:
        proposal, status = Proposal.WAIT, SignalStatus.STUDY
        reasons.append("El beneficio esperado no cubre costes y riesgo con margen suficiente.")
    if blocked:
        status = SignalStatus.BLOCKED

    return Evaluation(
        Signal(symbol=inp.symbol, name=inp.name, price=_q(price), change_pct=change, freshness=inp.freshness,
               trend=trend, proposal=proposal, horizon_minutes=h.minutes,
               net_eval_after_costs=Money(amount=net_eur, currency="EUR") if net_eur is not None else None,
               status=status, reasons=reasons, contradictions=contradictions,
               model_version=forecast.model_version),
        horizons=stats, data_id=data_id,
    )
