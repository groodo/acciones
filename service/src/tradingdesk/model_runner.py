"""Ejecución local de Kronos-small (PRD §8). Nunca toca órdenes ni el bróker.

El código de Kronos no se publica en PyPI: ``scripts/install_kronos.sh`` clona el
repositorio oficial en un commit fijado y descarga los pesos de Hugging Face en
la carpeta de datos, dejando un ``kronos.lock.json`` con el commit y el SHA-256
de cada archivo. Al cargar se comprueban esos hashes; si no coinciden, el modelo
no se usa y las señales quedan en ``No evaluable``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from .forecast import Candle, ForecastResult
from .logging_setup import log_event

log = logging.getLogger("tradingdesk.model")

MODEL_ID = "NeoQuasar/Kronos-small"
TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
MAX_CONTEXT = 512


class ModelUnavailable(RuntimeError):
    """Kronos no está instalado, no supera la verificación o falló la inferencia."""


class Forecaster(Protocol):
    def status(self) -> "ModelStatus": ...
    def forecast(self, candles: list[Candle], pred_len: int, samples: int, bar_minutes: int) -> ForecastResult: ...


@dataclass(frozen=True)
class ModelStatus:
    installed: bool
    loaded: bool
    model_id: str
    version: str | None
    device: str | None
    detail: str
    last_latency_ms: int | None = None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_lock(kronos_dir: Path) -> dict:
    """Lee ``kronos.lock.json`` y comprueba el hash de cada archivo de pesos. Lanza ``ModelUnavailable``."""
    lock_path = kronos_dir / "kronos.lock.json"
    if not lock_path.exists():
        raise ModelUnavailable("Kronos no está instalado. Ejecuta scripts/install_kronos.sh.")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    for rel, expected in lock.get("files", {}).items():
        path = kronos_dir / rel
        if not path.exists() or sha256_file(path) != expected:
            raise ModelUnavailable(f"El archivo {rel} de Kronos falta o no coincide con su hash: modelo no usado.")
    if not lock.get("files"):
        raise ModelUnavailable("kronos.lock.json no contiene hashes de pesos.")
    return lock


class KronosForecaster:
    """Carga perezosa de Kronos-small + tokenizer y predicción por muestras independientes."""

    def __init__(self, kronos_dir: Path, device: str | None = None):
        self.kronos_dir = kronos_dir
        self._device = device
        self._predictor = None
        self._lock = threading.Lock()
        self._version: str | None = None
        self._last_latency: int | None = None
        self._error: str | None = None

    def status(self) -> ModelStatus:
        installed = (self.kronos_dir / "kronos.lock.json").exists()
        detail = self._error or ("Cargado" if self._predictor else
                                 "Instalado; se carga en la primera previsión" if installed else
                                 "No instalado: ejecuta scripts/install_kronos.sh")
        return ModelStatus(installed=installed, loaded=self._predictor is not None, model_id=MODEL_ID,
                           version=self._version, device=self._device, detail=detail,
                           last_latency_ms=self._last_latency)

    def _load(self):
        if self._predictor is not None:
            return self._predictor
        try:
            lock = verify_lock(self.kronos_dir)
            src = self.kronos_dir / "src"
            if str(src) not in sys.path:
                sys.path.insert(0, str(src))
            import torch  # noqa: PLC0415
            from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: PLC0415

            device = self._device or ("mps" if torch.backends.mps.is_available() else "cpu")
            tokenizer = KronosTokenizer.from_pretrained(str(self.kronos_dir / "weights" / "tokenizer"))
            model = Kronos.from_pretrained(str(self.kronos_dir / "weights" / "model"))
            self._predictor = KronosPredictor(model, tokenizer, device=device, max_context=MAX_CONTEXT)
            self._device = device
            self._version = f"{MODEL_ID}@{lock.get('repo_commit', '?')[:10]}/{lock.get('model_revision', '?')[:10]}"
            self._error = None
            log_event(log, "Kronos cargado", device=device, version=self._version)
            return self._predictor
        except ModelUnavailable as exc:
            self._error = str(exc)
            raise
        except Exception as exc:  # noqa: BLE001
            self._error = f"No se pudo cargar Kronos: {exc}"
            raise ModelUnavailable(self._error) from exc

    def forecast(self, candles: list[Candle], pred_len: int, samples: int, bar_minutes: int) -> ForecastResult:
        import pandas as pd  # noqa: PLC0415

        with self._lock:
            predictor = self._load()
            ctx = candles[-(MAX_CONTEXT - pred_len):]
            # amount = volumen × precio medio: estimación documentada, IBKR no da importe negociado por vela.
            df = pd.DataFrame({
                "open": [c.open for c in ctx], "high": [c.high for c in ctx], "low": [c.low for c in ctx],
                "close": [c.close for c in ctx], "volume": [c.volume for c in ctx],
                "amount": [c.volume * (c.open + c.high + c.low + c.close) / 4 for c in ctx],
            })
            x_ts = pd.Series(pd.to_datetime([c.ts for c in ctx], utc=True).tz_localize(None))
            step = timedelta(minutes=bar_minutes)
            y_ts = pd.Series(pd.to_datetime([ctx[-1].ts + step * (i + 1) for i in range(pred_len)],
                                            utc=True).tz_localize(None))
            started = time.perf_counter()
            paths = []
            try:
                for _ in range(samples):
                    pred = predictor.predict(df=df, x_timestamp=x_ts, y_timestamp=y_ts, pred_len=pred_len,
                                             T=1.0, top_p=0.9, sample_count=1)
                    paths.append([float(v) for v in pred["close"].tolist()])
            except Exception as exc:  # noqa: BLE001
                self._error = f"Kronos no ha completado la predicción: {exc}"
                raise ModelUnavailable(self._error) from exc
            self._last_latency = int((time.perf_counter() - started) * 1000)
            return ForecastResult(model_id=MODEL_ID, model_version=self._version or MODEL_ID, paths=paths,
                                  latency_ms=self._last_latency, device=self._device or "?")
