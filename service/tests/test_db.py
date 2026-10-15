from tradingdesk.db import MIGRATIONS, Database


def test_wal_and_migrations(tmp_path):
    db = Database(tmp_path / "a.sqlite3")
    assert db.journal_mode.lower() == "wal"
    assert db.migrate() == MIGRATIONS[-1][0]
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"orders", "order_events", "account_snapshots", "predictions", "audit_log", "settings"} <= tables
    db.close()


def test_migrate_is_idempotent_and_incremental(tmp_path):
    path = tmp_path / "b.sqlite3"
    db = Database(path)
    db.migrate()
    db.close()
    db = Database(path)
    extra = MIGRATIONS + [(MIGRATIONS[-1][0] + 1, "CREATE TABLE extra (id INTEGER PRIMARY KEY)")]
    assert db.migrate(extra) == MIGRATIONS[-1][0] + 1
    assert db.migrate(extra) == MIGRATIONS[-1][0] + 1
    db.close()


def test_failed_migration_rolls_back(tmp_path):
    db = Database(tmp_path / "c.sqlite3")
    db.migrate()
    before = db.schema_version()
    bad = MIGRATIONS + [(before + 1, "CREATE TABLE ok_table (id INTEGER); THIS IS NOT SQL")]
    try:
        db.migrate(bad)
    except Exception:
        pass
    assert db.schema_version() == before
    assert not db.query("SELECT name FROM sqlite_master WHERE name = 'ok_table'")
    db.close()
