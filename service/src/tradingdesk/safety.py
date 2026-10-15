"""Kill switch persistente y reloj de mercado."""

from __future__ import annotations

import json
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from .db import Database

NY = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


def market_is_open(now_utc: datetime) -> bool:
    """Sesión regular NYSE/Nasdaq (lun-vie 9:30-16:00 hora de Nueva York).

    Limitación conocida de Hito A: no contempla festivos ni cierres anticipados.
    El horario de verano lo resuelve ``zoneinfo``.
    """
    if now_utc.tzinfo is None:
        raise ValueError("Se requiere una fecha con zona horaria (UTC)")
    local = now_utc.astimezone(NY)
    if local.weekday() >= 5:
        return False
    return REGULAR_OPEN <= local.time() < REGULAR_CLOSE


class KillSwitch:
    """Bloquea nuevas entradas de inmediato. No liquida posiciones por sí mismo (SAFETY-01).

    El estado vive en SQLite, así que sobrevive a reinicios del servicio.
    """

    KEY = "kill_switch"

    def __init__(self, db: Database):
        self.db = db

    def state(self) -> dict:
        rows = self.db.query("SELECT value FROM settings WHERE key = ?", (self.KEY,))
        if not rows:
            return {"active": False, "reason": None, "changed_at": None}
        return json.loads(rows[0]["value"])

    @property
    def active(self) -> bool:
        return bool(self.state()["active"])

    def set(self, active: bool, reason: str | None, actor: str = "user") -> dict:
        now = datetime.now(timezone.utc).isoformat()
        value = {"active": active, "reason": reason, "changed_at": now}
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (self.KEY, json.dumps(value), now),
            )
            conn.execute(
                "INSERT INTO audit_log (at, actor, action, detail) VALUES (?, ?, ?, ?)",
                (now, actor, "kill_switch.activated" if active else "kill_switch.released",
                 json.dumps({"reason": reason}, ensure_ascii=False)),
            )
        return value
