"""Persistencia SQLite en modo WAL con migraciones versionadas.

Los importes se guardan como TEXT (Decimal) para no perder precisión contable.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Cada migración se aplica una vez, en orden, dentro de una transacción.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL
        );

        CREATE TABLE account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            taken_at TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
            source TEXT NOT NULL CHECK (source IN ('MOCK', 'IBKR')),
            is_mock INTEGER NOT NULL CHECK (is_mock IN (0, 1)),
            payload TEXT NOT NULL
        );

        CREATE TABLE orders (
            client_order_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
            is_mock INTEGER NOT NULL CHECK (is_mock IN (0, 1)),
            symbol TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
            quantity TEXT NOT NULL,
            limit_price TEXT NOT NULL,
            currency TEXT NOT NULL,
            state TEXT NOT NULL,
            filled_quantity TEXT NOT NULL DEFAULT '0',
            avg_fill_price TEXT,
            commission_amount TEXT,
            commission_currency TEXT,
            ibkr_order_id INTEGER,
            ibkr_perm_id INTEGER,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
            from_state TEXT,
            to_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            at TEXT NOT NULL
        );
        CREATE INDEX idx_order_events_order ON order_events(client_order_id, id);

        CREATE TABLE predictions (
            id TEXT PRIMARY KEY,
            model_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            symbol TEXT NOT NULL,
            input_cutoff TEXT NOT NULL,
            candles INTEGER NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            payload TEXT NOT NULL,
            calibrated INTEGER NOT NULL DEFAULT 0,
            data_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        ALTER TABLE orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'LMT';
        ALTER TABLE orders ADD COLUMN parent_client_order_id TEXT;
        """,
    ),
]


class Database:
    """Una conexión compartida protegida por un lock (el servicio es local y de un usuario)."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self.journal_mode = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        self._conn.execute("PRAGMA synchronous = NORMAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self, migrations: list[tuple[int, str]] | None = None) -> int:
        migrations = MIGRATIONS if migrations is None else migrations
        current = self.schema_version()
        for version, script in sorted(migrations):
            if version <= current:
                continue
            with self.transaction() as conn:
                for statement in _split_statements(script):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {int(version)}")
            current = version
        return current


def _split_statements(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
