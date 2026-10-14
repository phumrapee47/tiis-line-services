import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# backend.config reads these at import time; real environment variables win over .env
for _key, _value in {"LINE_CHANNEL_SECRET": "", "LINE_CHANNEL_TOKEN": "", "TEAM_TOKEN": "", "DEMO_TODAY": "",
                     "PROJECT_START": "2026-10-19", "DB_PATH": ":memory:", "DATABASE_URL": "",
                     "TEAM_AUTH": "on"}.items():
    os.environ[_key] = _value

from backend import config  # noqa: E402
from backend.bot import Bot  # noqa: E402
from backend.db import Database  # noqa: E402

# Postgres is optional: set TEST_DATABASE_URL (in .env) to also run tests/test_db_contract.py against it.
# It only ever touches the throwaway schema "tiis_test", never `public`.
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")
SCHEMA_SQL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "postgres_schema.sql")


class Clock:
    def __init__(self):
        self.d = config.PROJECT_START + timedelta(days=1)  # week 1


class Chat:
    """Plays one learner: say() types text, tap() presses a quick-reply button by its label."""

    def __init__(self, bot, uid="U1"):
        self.bot, self.uid, self.last = bot, uid, []

    def event(self, ev):
        self.last = self.bot.handle(self.uid, ev)
        return self.last

    def say(self, text):
        return self.event({"type": "text", "text": text})

    def tap(self, label):
        for m in self.last:
            for q in m.get("quick_replies", []):
                if label in q["label"]:
                    return self.event({"type": "postback", "data": q["data"]})
        raise AssertionError(f"no button {label!r} in {[m['text'] for m in self.last]}")

    @property
    def text(self):
        return "\n".join(m["text"] for m in self.last)

    @property
    def buttons(self):
        return [q["label"] for m in self.last for q in m.get("quick_replies", [])]


@pytest.fixture(scope="session")
def pg_database():
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    import psycopg
    from backend.pg import PostgresDatabase
    with psycopg.connect(TEST_DB_URL, autocommit=True) as admin:
        admin.execute("DROP SCHEMA IF EXISTS tiis_test CASCADE")
        admin.execute("CREATE SCHEMA tiis_test")
    pg = PostgresDatabase(TEST_DB_URL, schema="tiis_test", seed=False)
    with open(SCHEMA_SQL, encoding="utf-8") as f:
        pg.apply_schema(f.read())
    yield pg
    pg.conn.close()
    with psycopg.connect(TEST_DB_URL, autocommit=True) as admin:   # leave nothing behind
        admin.execute("DROP SCHEMA IF EXISTS tiis_test CASCADE")


@pytest.fixture(params=["sqlite"] + (["postgres"] if TEST_DB_URL else []))
def any_db(request):
    """The storage interface on every configured backend (SQLite always, Postgres when TEST_DATABASE_URL is set)."""
    if request.param == "sqlite":
        return Database(":memory:")
    pg = request.getfixturevalue("pg_database")
    pg.reset()
    return pg


@pytest.fixture
def any_bot(any_db, clock):
    return Bot(any_db, today=lambda: clock.d)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db():
    return Database(":memory:")


@pytest.fixture
def bot(db, clock):
    return Bot(db, today=lambda: clock.d)


@pytest.fixture
def app_client(db, bot, monkeypatch):
    """Flask test client with the team token already set (and sent on every request)."""
    from backend.app import create_app
    monkeypatch.setattr(config, "TEAM_TOKEN", "team-test-token")
    client = create_app(db, bot).test_client()
    client.environ_base["HTTP_X_TEAM_TOKEN"] = "team-test-token"
    return client


def register(bot, uid="U1", nickname="เอ"):
    c = Chat(bot, uid)
    c.say("สวัสดี")
    c.tap("ยินยอม")
    c.say(nickname)
    c.tap("ฝึกทักษะ")
    return c
