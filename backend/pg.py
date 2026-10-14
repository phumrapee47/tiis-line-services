"""PostgreSQL (Supabase) storage: same interface as db.Database, schema in db/postgres_schema.sql.

The rest of the app identifies learners by their LINE user id (a string). Here that is the
`users.line_user_id` column; every other table points at the internal `users.id` UUID, so each method
resolves the id first. Timestamps come back as UTC ISO strings and dates as ISO strings, like SQLite."""
import threading
from datetime import date, datetime, timezone

import psycopg
from psycopg.errors import UniqueViolation  # noqa: F401  (re-exported for callers that want to catch it)
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .tasks import DEFAULT_TASKS

_USER = """SELECT line_user_id AS user_id, nickname, goal, display_name, consented_at, flow_state AS state,
                  flow_data::text AS state_data, joined_on, blocked_at, created_at, updated_at FROM users"""
_CHECKIN = """SELECT u.line_user_id AS user_id, c.week, c.progress::text AS progress, c.confidence,
                     CASE WHEN c.blocker IS NULL THEN NULL
                          WHEN c.blocker_note IS NOT NULL THEN c.blocker::text || ': ' || c.blocker_note
                          ELSE c.blocker::text END AS blocker,
                     c.wants_contact, c.task_title, c.created_at, c.updated_at
              FROM checkins c JOIN users u ON u.id = c.user_id"""
_HELP = """SELECT h.id, u.line_user_id AS user_id, h.category::text AS category, h.status::text AS status,
                  (SELECT string_agg(n.body, ' | ' ORDER BY n.id) FROM help_notes n WHERE n.help_id = h.id) AS detail,
                  h.created_at, h.updated_at
           FROM help_requests h JOIN users u ON u.id = h.user_id"""
_USER_FIELDS = {"nickname": "%s", "goal": "%s", "display_name": "%s", "consented_at": "%s::timestamptz",
                "blocked_at": "%s::timestamptz"}
_HELP_FIELDS = {"status": "%s::help_status_t", "created_at": "%s::timestamptz"}
_TABLES = ("outbox", "pushes_sent", "followups", "help_status_log", "help_notes", "help_requests",
           "checkins", "webhook_events", "users", "weekly_tasks")


def _plain(row):
    """Make a row look like the SQLite one: ISO strings for dates/timestamps."""
    if row is None:
        return None
    out = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            value = value.astimezone(timezone.utc).isoformat(timespec="seconds")
        elif isinstance(value, date):
            value = value.isoformat()
        out[key] = value
    return out


def _split_blocker(blocker):
    """The bot stores 'other: free text'; the schema keeps the code and the note apart."""
    if blocker is None:
        return None, None
    if blocker.split(":")[0] == "other":
        return "other", blocker.partition(":")[2].strip() or None
    return blocker, None


class PostgresDatabase:
    def __init__(self, url, schema=None, seed=True):
        self.url, self.schema = url, schema
        self.lock = threading.RLock()
        self.conn = None
        self._connect()
        if seed:
            self._seed_tasks()

    # ---- connection ------------------------------------------------------------------
    def _connect(self):
        opts = {"options": f"-c search_path={self.schema}"} if self.schema else {}
        # prepare_threshold=None: Supabase's transaction pooler does not support prepared statements
        self.conn = psycopg.connect(self.url, autocommit=True, row_factory=dict_row, prepare_threshold=None,
                                    connect_timeout=10, **opts)

    def _run(self, sql, args=()):
        """Returns (rows, rowcount). Reconnects once if the pooler dropped an idle connection."""
        with self.lock:
            for attempt in (1, 2):
                try:
                    cur = self.conn.execute(sql, args)
                    rows = cur.fetchall() if cur.description else []
                    return [_plain(r) for r in rows], cur.rowcount
                except (psycopg.OperationalError, psycopg.InterfaceError):
                    if attempt == 2:
                        raise
                    self._connect()

    def _q(self, sql, args=()):
        return self._run(sql, args)[0]

    def _one(self, sql, args=()):
        rows = self._q(sql, args)
        return rows[0] if rows else None

    def _uuid(self, user_id):
        row = self._one("SELECT id FROM users WHERE line_user_id = %s", (user_id,))
        if row is None:
            raise LookupError(f"unknown user {user_id!r}")
        return row["id"]

    # ---- schema / test support -------------------------------------------------------
    def apply_schema(self, sql_text):
        with self.lock:
            self.conn.execute(sql_text)

    def reset(self):
        """Empty every table (tests only) and put the default tasks back."""
        with self.lock:
            self.conn.execute("TRUNCATE " + ", ".join(_TABLES) + " RESTART IDENTITY CASCADE")
        self._seed_tasks()

    def _seed_tasks(self):
        if self._one("SELECT 1 AS x FROM weekly_tasks LIMIT 1") is None:
            for week, (title, desc) in DEFAULT_TASKS.items():
                self._run("INSERT INTO weekly_tasks (week, title, description) VALUES (%s, %s, %s)",
                          (week, title, desc))

    # ---- users -----------------------------------------------------------------------
    def get_user(self, user_id):
        return self._one(_USER + " WHERE line_user_id = %s", (user_id,))

    def create_user(self, user_id, joined_on, state="consent"):
        self._run("INSERT INTO users (line_user_id, joined_on, flow_state) VALUES (%s, %s, %s)",
                  (user_id, date.fromisoformat(joined_on), state))

    def set_state(self, user_id, state, data=None):
        self._run("UPDATE users SET flow_state = %s, flow_data = %s WHERE line_user_id = %s",
                  (state, Jsonb(data or {}), user_id))

    def update_user(self, user_id, **fields):
        unknown = set(fields) - set(_USER_FIELDS)
        if unknown:
            raise ValueError(f"cannot update {sorted(unknown)}")
        sets = ", ".join(f"{col} = {_USER_FIELDS[col]}" for col in fields)
        self._run(f"UPDATE users SET {sets} WHERE line_user_id = %s", (*fields.values(), user_id))

    def delete_user_data(self, user_id):
        self._run("DELETE FROM users WHERE line_user_id = %s", (user_id,))   # children cascade

    def all_users(self):
        return self._q(_USER + " ORDER BY joined_on, line_user_id")

    # ---- webhook de-duplication ------------------------------------------------------
    def mark_event(self, event_id) -> bool:
        return self._run("INSERT INTO webhook_events (event_id) VALUES (%s) ON CONFLICT DO NOTHING",
                         (event_id,))[1] == 1

    def unmark_event(self, event_id):
        self._run("DELETE FROM webhook_events WHERE event_id = %s", (event_id,))

    # ---- check-ins -------------------------------------------------------------------
    def get_checkin(self, user_id, week):
        return self._one(_CHECKIN + " WHERE u.line_user_id = %s AND c.week = %s", (user_id, week))

    def upsert_checkin(self, user_id, week, progress, confidence, blocker, wants_contact, task_title=None):
        code, note = _split_blocker(blocker)
        self._run(
            """INSERT INTO checkins (user_id, week, progress, confidence, blocker, blocker_note, wants_contact,
                                     task_title)
               VALUES (%s, %s, %s::progress_t, %s, %s::blocker_t, %s, %s, %s)
               ON CONFLICT (user_id, week) DO UPDATE SET progress = EXCLUDED.progress,
                 confidence = EXCLUDED.confidence, blocker = EXCLUDED.blocker,
                 blocker_note = EXCLUDED.blocker_note, wants_contact = EXCLUDED.wants_contact,
                 task_title = EXCLUDED.task_title""",
            (self._uuid(user_id), week, progress, confidence, code, note, bool(wants_contact), task_title))

    def checkin_weeks(self, user_id):
        return {r["week"] for r in self._q(
            "SELECT c.week FROM checkins c JOIN users u ON u.id = c.user_id WHERE u.line_user_id = %s", (user_id,))}

    def all_checkins(self):
        return self._q(_CHECKIN + " ORDER BY c.week, u.line_user_id")

    def latest_checkin(self, user_id):
        return self._one(_CHECKIN + " WHERE u.line_user_id = %s ORDER BY c.week DESC LIMIT 1", (user_id,))

    def count_pushes(self, kind):
        return self._one("SELECT count(*)::int AS n FROM pushes_sent WHERE kind = %s", (kind,))["n"]

    # ---- help requests ---------------------------------------------------------------
    def create_help(self, user_id, category, detail):
        uid = self._uuid(user_id)
        with self.lock, self.conn.transaction():
            help_id = self.conn.execute(
                "INSERT INTO help_requests (user_id, category) VALUES (%s, %s::help_cat_t) RETURNING id",
                (uid, category)).fetchone()["id"]
            if detail:
                self.conn.execute("INSERT INTO help_notes (help_id, author, body) VALUES (%s, 'learner', %s)",
                                  (help_id, detail))
            self.conn.execute("INSERT INTO help_status_log (help_id, from_status, to_status, changed_by) "
                              "VALUES (%s, NULL, 'waiting', 'learner')", (help_id,))
        return help_id

    def get_help(self, help_id):
        return self._one(_HELP + " WHERE h.id = %s", (help_id,))

    def open_help(self, user_id, category=None):
        sql = _HELP + " WHERE u.line_user_id = %s AND h.status IN ('waiting', 'accepted')"
        args = [user_id]
        if category:
            sql += " AND h.category = %s::help_cat_t"
            args.append(category)
        return self._q(sql + " ORDER BY h.id", args)

    def user_help(self, user_id, limit=3):
        return self._q(_HELP + " WHERE u.line_user_id = %s ORDER BY h.id DESC LIMIT %s", (user_id, limit))

    def list_help(self, status=None):
        if status:
            return self._q(_HELP + " WHERE h.status = %s::help_status_t ORDER BY h.id", (status,))
        return self._q(_HELP + " ORDER BY h.id")

    def update_help(self, help_id, **fields):
        changed_by = fields.pop("changed_by", "system")
        unknown = set(fields) - set(_HELP_FIELDS)
        if unknown:
            raise ValueError(f"cannot update {sorted(unknown)}")
        if not fields:
            return
        with self.lock, self.conn.transaction():
            old = self.conn.execute("SELECT status::text AS status FROM help_requests WHERE id = %s FOR UPDATE",
                                    (help_id,)).fetchone()
            sets = ", ".join(f"{col} = {_HELP_FIELDS[col]}" for col in fields)
            self.conn.execute(f"UPDATE help_requests SET {sets} WHERE id = %s", (*fields.values(), help_id))
            if old and "status" in fields and fields["status"] != old["status"]:
                self.conn.execute("INSERT INTO help_status_log (help_id, from_status, to_status, changed_by) "
                                  "VALUES (%s, %s::help_status_t, %s::help_status_t, %s)",
                                  (help_id, old["status"], fields["status"], changed_by))

    def add_help_note(self, help_id, body, author="learner"):
        self._run("INSERT INTO help_notes (help_id, author, body) VALUES (%s, %s, %s)", (help_id, author, body))

    # ---- outbox / scheduled pushes / follow-ups ---------------------------------------
    def add_outbox(self, user_id, messages):
        self._run("INSERT INTO outbox (user_id, messages) VALUES (%s, %s)", (self._uuid(user_id), Jsonb(messages)))

    def pending_outbox(self, user_id=None):
        sql = ("SELECT o.id, u.line_user_id AS user_id, o.messages, o.attempts, o.last_error, o.created_at "
               "FROM outbox o JOIN users u ON u.id = o.user_id WHERE o.delivered_at IS NULL")
        args = ()
        if user_id:
            sql, args = sql + " AND u.line_user_id = %s", (user_id,)
        return self._q(sql + " ORDER BY o.id", args)

    def mark_delivered(self, outbox_id):
        self._run("UPDATE outbox SET delivered_at = now() WHERE id = %s", (outbox_id,))

    def record_outbox_failure(self, outbox_id, error):
        self._run("UPDATE outbox SET attempts = attempts + 1, last_error = %s WHERE id = %s",
                  (str(error)[:500], outbox_id))

    def mark_push(self, user_id, kind, key) -> bool:
        return self._run("INSERT INTO pushes_sent (user_id, kind, key) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                         (self._uuid(user_id), kind, str(key)))[1] == 1

    def save_followup(self, user_id, kind, outcome):
        self._run("INSERT INTO followups (user_id, kind, outcome) VALUES (%s, %s, %s::followup_t) "
                  "ON CONFLICT (user_id, kind) DO UPDATE SET outcome = EXCLUDED.outcome, created_at = now()",
                  (self._uuid(user_id), kind, outcome))

    def list_followups(self):
        return self._q("SELECT u.line_user_id AS user_id, f.kind, f.outcome::text AS outcome, f.created_at "
                       "FROM followups f JOIN users u ON u.id = f.user_id ORDER BY u.line_user_id, f.kind")

    # ---- weekly tasks ----------------------------------------------------------------
    def get_task(self, week):
        return self._one("SELECT week, title, description, updated_at FROM weekly_tasks WHERE week = %s", (week,))

    def list_tasks(self):
        return self._q("SELECT week, title, description, updated_at FROM weekly_tasks ORDER BY week")

    def save_task(self, week, title, description):
        self._run("INSERT INTO weekly_tasks (week, title, description) VALUES (%s, %s, %s) "
                  "ON CONFLICT (week) DO UPDATE SET title = EXCLUDED.title, description = EXCLUDED.description",
                  (week, title, description))
