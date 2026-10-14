"""Project settings. Dates are assumptions for the pilot; override with env vars."""
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def _load_dotenv(path):
    """Tiny .env reader (KEY=VALUE, # comments). Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DB_PATH = os.environ.get("DB_PATH", "data.db")
# Postgres/Supabase connection string (use the Session pooler URI on Render). Empty = SQLite at DB_PATH.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Day 1 of the 6-week pilot (assumed, not given in the brief).
PROJECT_START = date.fromisoformat(os.environ.get("PROJECT_START", "2026-10-19"))
PROJECT_DAYS = 42            # 6 weeks
MAIN_ACTIVITY_DAYS = 28      # weeks 1-4: main learning activity
FOLLOWUP_MIN_DAYS = 14       # brief: follow up >= 14 days after the main activity

MAIN_ACTIVITY_END = PROJECT_START + timedelta(days=MAIN_ACTIVITY_DAYS - 1)
PROJECT_END = PROJECT_START + timedelta(days=PROJECT_DAYS - 1)
assert MAIN_ACTIVITY_END + timedelta(days=FOLLOWUP_MIN_DAYS) <= PROJECT_END, (
    "follow-up window must fit inside the project"
)

# Promised reply time shown to learners (assumption, not a measured SLA).
SLA_TEXT = "ภายใน 1 วันทำการ"

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN", "")
# Optional: pretend today is this date (demo of weeks 5-6 without waiting).
DEMO_TODAY = os.environ.get("DEMO_TODAY", "")


THAI_TZ = timezone(timedelta(hours=7))


def restricted_instance() -> bool:
    """An instance that can message real learners (LINE token) or holds real data (DATABASE_URL)."""
    return bool(LINE_CHANNEL_TOKEN or DATABASE_URL)


def today() -> date:
    return date.fromisoformat(DEMO_TODAY) if DEMO_TODAY else datetime.now(THAI_TZ).date()


def in_checkin_period(d: date) -> bool:
    """Weekly reports happen during the main activity (weeks 1-4). Before the start date is allowed
    so the bot can be tried out in advance; weeks 5-6 are follow-up only."""
    return (d - PROJECT_START).days < MAIN_ACTIVITY_DAYS


def week_no(d: date) -> int:
    """Project week 1..6 (clamped so demos outside the window still work)."""
    return min(max((d - PROJECT_START).days // 7 + 1, 1), PROJECT_DAYS // 7)
