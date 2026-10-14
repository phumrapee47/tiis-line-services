import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# backend.config reads these at import time; real environment variables win over .env
for _key, _value in {"LINE_CHANNEL_SECRET": "", "LINE_CHANNEL_TOKEN": "", "TEAM_TOKEN": "", "DEMO_TODAY": "",
                     "PROJECT_START": "2026-10-19", "DB_PATH": ":memory:"}.items():
    os.environ[_key] = _value

from backend import config  # noqa: E402
from backend.bot import Bot  # noqa: E402
from backend.db import Database  # noqa: E402


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
