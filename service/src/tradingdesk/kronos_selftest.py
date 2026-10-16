"""Inferencia de prueba de Kronos con una serie sintética: ``python -m tradingdesk.kronos_selftest``.

Solo comprueba que el modelo carga y responde, y mide la latencia. No dice nada de su calidad.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone

from .config import Settings
from .forecast import Candle
from .model_runner import KronosForecaster, ModelUnavailable


def main() -> int:
    settings = Settings.from_env()
    start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    candles = []
    for i in range(240):
        p = 100 + 2 * math.sin(i / 15) + i * 0.01
        candles.append(Candle(start + timedelta(minutes=5 * i), p, p + 0.3, p - 0.3, p + 0.05, 1000 + 10 * (i % 7)))
    runner = KronosForecaster(settings.kronos_dir)
    try:
        result = runner.forecast(candles, pred_len=12, samples=3, bar_minutes=5)
    except ModelUnavailable as exc:
        print(f"FALLO: {exc}", file=sys.stderr)
        return 1
    print(f"Kronos OK · {result.model_version} · dispositivo {result.device} · "
          f"{len(result.paths)} muestras en {result.latency_ms} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
