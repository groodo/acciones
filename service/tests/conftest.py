from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tradingdesk.api import create_app
from tradingdesk.config import Settings
from tradingdesk.db import Database
from tradingdesk.mock_data import MockProvider
from tradingdesk.orders import OrderStore

TOKEN = "t" * 43
FIXED_NOW = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def store(db):
    return OrderStore(db)


@pytest.fixture
def client(settings):
    app = create_app(settings, token=TOKEN, provider=MockProvider(now=FIXED_NOW))
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c
    app.state.db.close()
