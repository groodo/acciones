"""Precios en tiempo real gratuitos de Alpaca (feed IEX) para Kronos. Solo lectura de datos de mercado.

IBKR sigue llevando la cuenta, el escáner y las órdenes. Alpaca solo aporta velas: su plan
gratuito da en tiempo real el mercado IEX, que tiene casi el mismo precio que el consolidado
pero solo una parte del volumen. Las claves se guardan en un archivo 0600 de la carpeta de
datos y nunca se devuelven por la API ni se escriben en los logs.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .domain import Freshness, Position
from .forecast import Candle
from .logging_setup import log_event

log = logging.getLogger("tradingdesk.alpaca")

DATA_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN, REGULAR_CLOSE = time(9, 30), time(16, 0)
IEX_NOTE = "Precios en tiempo real de IEX vía Alpaca (un solo mercado: el volumen es parcial)."


class AlpacaError(RuntimeError):
    """Alpaca no ha dado datos (claves no válidas, límite de peticiones, sin red…)."""


def load_keys(path: Path) -> tuple[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data["key_id"], data["secret_key"]
    except (OSError, ValueError, KeyError):
        return None


def save_keys(path: Path, key_id: str, secret_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"key_id": key_id, "secret_key": secret_key}, fh)
    os.chmod(path, 0o600)


def mask_key(key_id: str) -> str:
    return key_id[:2] + "…" + key_id[-4:] if len(key_id) > 6 else "…"


def _default_get(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - URL fija de Alpaca
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AlpacaError("Alpaca rechaza las claves de API (revisa que estén bien copiadas).") from exc
        if exc.code == 429:
            raise AlpacaError("Alpaca: demasiadas peticiones seguidas; se reintenta en el próximo ciclo.") from exc
        raise AlpacaError(f"Alpaca respondió con el error {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AlpacaError(f"No se puede conectar con Alpaca: {exc}") from exc


class AlpacaData:
    def __init__(self, key_id: str, secret_key: str, *, get: Callable[[str, dict], dict] = _default_get,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self._get = get
        self.clock = clock

    def bars(self, symbol: str, bar_minutes: int, days: int = 7) -> list[Candle]:
        """Velas cerradas de la sesión regular (9:30–16:00 Nueva York) de los últimos días, feed IEX."""
        now = self.clock()
        params = {"timeframe": f"{bar_minutes}Min", "start": (now - timedelta(days=days)).isoformat(),
                  "feed": "iex", "adjustment": "raw", "limit": 10000}
        candles: list[Candle] = []
        token = None
        for _ in range(10):
            query = dict(params, **({"page_token": token} if token else {}))
            url = DATA_URL.format(symbol=urllib.parse.quote(symbol)) + "?" + urllib.parse.urlencode(query)
            data = self._get(url, self._headers)
            for b in data.get("bars") or []:
                ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
                local = ts.astimezone(NEW_YORK).time()
                if not (REGULAR_OPEN <= local < REGULAR_CLOSE):
                    continue
                if ts + timedelta(minutes=bar_minutes) > now:
                    continue  # vela aún abierta
                candles.append(Candle(ts=ts, open=float(b["o"]), high=float(b["h"]), low=float(b["l"]),
                                      close=float(b["c"]), volume=float(b["v"])))
            token = data.get("next_page_token")
            if not token:
                break
        return candles


class HybridSource:
    """Velas de Alpaca (tiempo real) y, para todo lo demás, IBKR: posiciones y escáner."""

    def __init__(self, broker, alpaca: AlpacaData):
        self.broker, self.alpaca = broker, alpaca
        self.data_note: str | None = None  # motivo que se añade a la señal según de dónde salieron las velas

    def historical_candles(self, symbol: str, bar_minutes: int) -> tuple[list[Candle], Freshness]:
        try:
            candles = self.alpaca.bars(symbol, bar_minutes)
            self.data_note = IEX_NOTE
            return candles, Freshness.REAL_TIME
        except AlpacaError as exc:
            log_event(log, "Alpaca no disponible: se usan las velas de IBKR", logging.WARNING, error=str(exc))
            self.data_note = f"Alpaca no respondió ({exc}): velas de IBKR."
            return self.broker.historical_candles(symbol, bar_minutes)

    def current_positions(self) -> list[Position]:
        return self.broker.current_positions()

    def ready(self) -> bool:
        ready = getattr(self.broker, "ready", None)
        return ready() if ready is not None else True

    def scan_candidates(self, n: int):
        return self.broker.scan_candidates(n)
