from datetime import datetime, timezone

import pytest

from tradingdesk.db import Database
from tradingdesk.safety import KillSwitch, market_is_open


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize("moment,expected", [
    (utc(2026, 7, 15, 13, 29), False),   # verano (EDT, UTC-4): 9:29 NY
    (utc(2026, 7, 15, 13, 30), True),    # 9:30 NY
    (utc(2026, 7, 15, 19, 59), True),    # 15:59 NY
    (utc(2026, 7, 15, 20, 0), False),    # 16:00 NY, cerrado
    (utc(2026, 12, 15, 14, 29), False),  # invierno (EST, UTC-5): 9:29 NY
    (utc(2026, 12, 15, 14, 30), True),
    (utc(2026, 3, 9, 13, 30), True),     # lunes tras cambio de hora en EE. UU. (8 mar 2026)
    (utc(2026, 10, 17, 15, 0), False),   # sábado
])
def test_market_hours_with_dst(moment, expected):
    assert market_is_open(moment) is expected


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        market_is_open(datetime(2026, 7, 15, 14, 0))


def test_kill_switch_persists_across_restart(tmp_path):
    path = tmp_path / "ks.sqlite3"
    db = Database(path)
    db.migrate()
    assert KillSwitch(db).active is False
    KillSwitch(db).set(True, "prueba")
    db.close()
    db = Database(path)
    assert KillSwitch(db).state()["reason"] == "prueba"
    assert KillSwitch(db).active is True
    db.close()
