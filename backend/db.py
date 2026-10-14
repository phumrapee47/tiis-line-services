"""SQLite storage. One shared connection guarded by a lock (small pilot, 60 users)."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

from .tasks import DEFAULT_TASKS

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY, nickname TEXT, goal TEXT, display_name TEXT, consented_at TEXT,
    state TEXT NOT NULL, state_data TEXT NOT NULL DEFAULT '{}',
    joined_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkins (
    user_id TEXT NOT NULL, week INTEGER NOT NULL,
    progress TEXT NOT NULL, confidence INTEGER NOT NULL CHECK (confidence BETWEEN 1 AND 5),
    blocker TEXT, wants_contact INTEGER NOT NULL DEFAULT 0, task_title TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, week)
);
CREATE TABLE IF NOT EXISTS help_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
    category TEXT NOT NULL, detail TEXT,
    status TEXT NOT NULL DEFAULT 'waiting',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weekly_tasks (
    week INTEGER PRIMARY KEY CHECK (week BETWEEN 1 AND 4),
    title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    start_date TEXT NOT NULL CHECK (start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
    messages TEXT NOT NULL, created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
CREATE TABLE IF NOT EXISTS pushes_sent (
    user_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, key)
);
CREATE TABLE IF NOT EXISTS followups (
    user_id TEXT NOT NULL, kind TEXT NOT NULL, outcome TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, kind)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str = ":memory:"):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._seed_tasks()

    def _migrate(self):
        # databases created before weekly tasks existed lack the snapshot column
        added = (("checkins", "task_title", "TEXT"), ("users", "display_name", "TEXT"),
                 ("users", "consented_at", "TEXT"), ("outbox", "attempts", "INTEGER NOT NULL DEFAULT 0"),
                 ("outbox", "last_error", "TEXT"))
        for table, column, ddl in added:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self.conn.commit()
        # files from the first version kept the start date in a key/value table: move it over, then drop that table
        old = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
        if old:
            row = self.conn.execute("SELECT value FROM settings WHERE key='project_start'").fetchone()
            if row and not self.conn.execute("SELECT 1 FROM project_config").fetchone():
                try:
                    self.conn.execute("INSERT INTO project_config (id, start_date, updated_at) VALUES (1,?,?)", (row["value"], _now()))
                except sqlite3.IntegrityError:
                    pass  # not a date: the team just sets it again
            self.conn.execute("DROP TABLE settings")
            self.conn.commit()

    def _seed_tasks(self):
        if not self.conn.execute("SELECT 1 FROM weekly_tasks LIMIT 1").fetchone():
            for week, (title, desc) in DEFAULT_TASKS.items():
                self.conn.execute(
                    "INSERT INTO weekly_tasks (week, title, description, updated_at) VALUES (?,?,?,?)",
                    (week, title, desc, _now()))
            self.conn.commit()

    def _q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def _x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    # users
    def get_user(self, user_id):
        rows = self._q("SELECT * FROM users WHERE user_id=?", (user_id,))
        return dict(rows[0]) if rows else None

    def create_user(self, user_id, joined_on, state="consent"):
        self._x("INSERT INTO users (user_id, state, joined_on) VALUES (?,?,?)",
                (user_id, state, joined_on))

    def set_state(self, user_id, state, data=None):
        self._x("UPDATE users SET state=?, state_data=? WHERE user_id=?",
                (state, json.dumps(data or {}, ensure_ascii=False), user_id))

    def update_user(self, user_id, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE users SET {cols} WHERE user_id=?", (*fields.values(), user_id))

    def delete_user_data(self, user_id):
        with self.lock:
            for t in ("users", "checkins", "help_requests", "outbox", "pushes_sent", "followups"):
                self.conn.execute(f"DELETE FROM {t} WHERE user_id=?", (user_id,))
            self.conn.commit()

    def all_users(self):
        return [dict(r) for r in self._q("SELECT * FROM users ORDER BY joined_on, user_id")]

    # idempotency: True the first time an event id is seen
    def mark_event(self, event_id) -> bool:
        try:
            self._x("INSERT INTO events (event_id, received_at) VALUES (?,?)", (event_id, _now()))
            return True
        except sqlite3.IntegrityError:
            return False

    def unmark_event(self, event_id):
        self._x("DELETE FROM events WHERE event_id=?", (event_id,))

    # check-ins
    def get_checkin(self, user_id, week):
        rows = self._q("SELECT * FROM checkins WHERE user_id=? AND week=?", (user_id, week))
        return dict(rows[0]) if rows else None

    def upsert_checkin(self, user_id, week, progress, confidence, blocker, wants_contact, task_title=None):
        now = _now()
        self._x(
            """INSERT INTO checkins (user_id, week, progress, confidence, blocker, wants_contact,
                                     task_title, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id, week) DO UPDATE SET
                 progress=excluded.progress, confidence=excluded.confidence,
                 blocker=excluded.blocker, wants_contact=excluded.wants_contact,
                 task_title=excluded.task_title,
                 updated_at=excluded.updated_at""",
            (user_id, week, progress, confidence, blocker, int(wants_contact), task_title, now, now))

    # the project start date
    def get_project_start(self):
        """ISO date string, or None while the team has not set one."""
        rows = self._q("SELECT start_date FROM project_config")
        return rows[0]["start_date"] if rows else None

    def save_project_start(self, iso):
        self._x("""INSERT INTO project_config (id, start_date, updated_at) VALUES (1,?,?)
                   ON CONFLICT(id) DO UPDATE SET start_date=excluded.start_date, updated_at=excluded.updated_at""",
                (str(iso), _now()))

    def has_checkins(self):
        return bool(self._q("SELECT 1 FROM checkins LIMIT 1"))

    # weekly tasks (set by the team)
    def get_task(self, week):
        rows = self._q("SELECT * FROM weekly_tasks WHERE week=?", (week,))
        return dict(rows[0]) if rows else None

    def list_tasks(self):
        return [dict(r) for r in self._q("SELECT * FROM weekly_tasks ORDER BY week")]

    def save_task(self, week, title, description):
        self._x("""INSERT INTO weekly_tasks (week, title, description, updated_at) VALUES (?,?,?,?)
                   ON CONFLICT(week) DO UPDATE SET title=excluded.title,
                   description=excluded.description, updated_at=excluded.updated_at""",
                (week, title, description, _now()))

    def checkin_weeks(self, user_id):
        return {r["week"] for r in self._q("SELECT week FROM checkins WHERE user_id=?", (user_id,))}

    def all_checkins(self):
        return [dict(r) for r in self._q("SELECT * FROM checkins ORDER BY week, user_id")]

    def count_pushes(self, kind):
        return self._q("SELECT COUNT(*) AS n FROM pushes_sent WHERE kind=?", (kind,))[0]["n"]

    def latest_checkin(self, user_id):
        rows = self._q("SELECT * FROM checkins WHERE user_id=? ORDER BY week DESC LIMIT 1", (user_id,))
        return dict(rows[0]) if rows else None

    # help requests
    def create_help(self, user_id, category, detail):
        now = _now()
        return self._x(
            "INSERT INTO help_requests (user_id, category, detail, created_at, updated_at) "
            "VALUES (?,?,?,?,?)", (user_id, category, detail, now, now)).lastrowid

    def get_help(self, help_id):
        rows = self._q("SELECT * FROM help_requests WHERE id=?", (help_id,))
        return dict(rows[0]) if rows else None

    def open_help(self, user_id, category=None):
        sql = "SELECT * FROM help_requests WHERE user_id=? AND status IN ('waiting','accepted')"
        args = [user_id]
        if category:
            sql += " AND category=?"
            args.append(category)
        return [dict(r) for r in self._q(sql + " ORDER BY id", args)]

    def user_help(self, user_id, limit=3):
        return [dict(r) for r in self._q(
            "SELECT * FROM help_requests WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))]

    def list_help(self, status=None):
        if status:
            rows = self._q("SELECT * FROM help_requests WHERE status=? ORDER BY id", (status,))
        else:
            rows = self._q("SELECT * FROM help_requests ORDER BY id")
        return [dict(r) for r in rows]

    def update_help(self, help_id, **fields):
        fields.pop("changed_by", None)  # only the Postgres backend keeps a status log
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE help_requests SET {cols} WHERE id=?", (*fields.values(), help_id))

    def add_help_note(self, help_id, body, author="learner"):
        h = self.get_help(help_id)
        merged = f"{h['detail']} | {body}" if h and h["detail"] else body
        self.update_help(help_id, detail=merged[:2000])

    # outbox (messages the system sends on its own: status updates, reminders)
    def add_outbox(self, user_id, messages):
        self._x("INSERT INTO outbox (user_id, messages, created_at) VALUES (?,?,?)",
                (user_id, json.dumps(messages, ensure_ascii=False), _now()))

    def pending_outbox(self, user_id=None):
        sql, args = "SELECT * FROM outbox WHERE delivered=0", ()
        if user_id:
            sql, args = sql + " AND user_id=?", (user_id,)
        return [{**dict(r), "messages": json.loads(r["messages"])} for r in self._q(sql + " ORDER BY id", args)]

    def record_outbox_failure(self, outbox_id, error):
        self._x("UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id=?", (str(error)[:500], outbox_id))

    def mark_delivered(self, outbox_id):
        self._x("UPDATE outbox SET delivered=1 WHERE id=?", (outbox_id,))

    # scheduled pushes and follow-ups
    def mark_push(self, user_id, kind, key) -> bool:
        try:
            self._x("INSERT INTO pushes_sent (user_id, kind, key) VALUES (?,?,?)", (user_id, kind, str(key)))
            return True
        except sqlite3.IntegrityError:
            return False

    def save_followup(self, user_id, kind, outcome):
        self._x("INSERT OR REPLACE INTO followups (user_id, kind, outcome, created_at) VALUES (?,?,?,?)",
                (user_id, kind, outcome, _now()))

    def list_followups(self):
        return [dict(r) for r in self._q("SELECT * FROM followups ORDER BY user_id, kind")]
