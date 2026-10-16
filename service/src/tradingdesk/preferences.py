"""Preferencias que el usuario cambia desde la app (tabla ``settings``): lista de seguimiento y tope por orden."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .db import Database

SYMBOL = re.compile(r"^[A-Z][A-Z.]{0,11}$")
MAX_WATCHLIST = 10


class PreferenceError(ValueError):
    """Valor no válido para una preferencia."""


class Preferences:
    def __init__(self, db: Database):
        self.db = db

    def _get(self, key: str) -> str | None:
        rows = self.db.query("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def _set(self, key: str, value: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, datetime.now(timezone.utc).isoformat()),
            )

    def watchlist(self, default: list[str]) -> list[str]:
        raw = self._get("watchlist")
        return json.loads(raw) if raw else list(default)[:MAX_WATCHLIST]

    def set_watchlist(self, symbols: list[str]) -> list[str]:
        clean: list[str] = []
        for s in symbols:
            s = s.strip().upper()
            if not SYMBOL.match(s):
                raise PreferenceError(f"«{s}» no es un símbolo válido (p. ej. AAPL o BRK.B).")
            if s not in clean:
                clean.append(s)
        if not 1 <= len(clean) <= MAX_WATCHLIST:
            raise PreferenceError(f"Tu lista debe tener entre 1 y {MAX_WATCHLIST} valores.")
        self._set("watchlist", json.dumps(clean))
        return clean

    def max_real_order_eur(self, default: Decimal, ceiling: Decimal) -> Decimal:
        raw = self._get("max_real_order_eur")
        value = Decimal(raw) if raw else default
        return min(value, ceiling)

    def set_max_real_order_eur(self, value: Decimal, ceiling: Decimal) -> Decimal:
        try:
            value = Decimal(value).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError) as exc:
            raise PreferenceError("Importe no válido.") from exc
        if not Decimal("1") <= value <= ceiling:
            raise PreferenceError(f"El tope por orden debe estar entre 1 € y {ceiling} €.")
        self._set("max_real_order_eur", str(value))
        return value
