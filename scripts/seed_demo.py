"""Fill a NEW SQLite file with made-up demo learners so the board has something to show.

    python scripts/seed_demo.py demo.db            # then:  DB_PATH=demo.db python -m backend.app
    python scripts/seed_demo.py demo.db --force    # overwrite an existing demo file

Everything here is invented for looking at the dashboard. It never touches Postgres/Supabase, never sends anything
to LINE (the ids start with "sim-"), and the project start date is set to 15 days ago so today is day 16 (week 3).
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["DATABASE_URL"] = ""          # this script only ever writes a local SQLite file
os.environ["LINE_CHANNEL_TOKEN"] = ""
os.environ["LINE_CHANNEL_SECRET"] = ""
os.environ["DEMO_TODAY"] = ""

from backend import config  # noqa: E402
from backend.bot import Bot  # noqa: E402
from backend.db import Database  # noqa: E402

PEOPLE = [  # (id, nickname, goal, LINE display name)
    ("sim-01", "มะลิ", "resume", "Mali"), ("sim-02", "ต้น", "skill", "Ton Kritsada"), ("sim-03", "ฟ้า", "interview", None),
    ("sim-04", "นุ่น", "resume", "Noon"), ("sim-05", "บอม", "skill", "Bomb"), ("sim-06", "ปุ้ย", "interview", None),
    ("sim-07", "โอ๊ต", "skill", "Oat"), ("sim-08", "แพร", "resume", "Praew K."), ("sim-09", "แพร", "skill", "แพร ✿"),
    ("sim-10", "ไผ่", "unsure", None), ("sim-11", "ขิง", "skill", "Ginger"),
]
ONBOARDING_ONLY = ("sim-12", "เนย")                  # registered but has not chosen a goal yet


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path, force = sys.argv[1], "--force" in sys.argv[2:]
    if os.path.exists(path):
        if not force:
            sys.exit(f"{path} already exists: pass --force to overwrite it (demo files only!)")
        os.remove(path)
    db = Database(path)
    today = datetime.now(config.THAI_TZ).date()
    start = today - timedelta(days=15)
    db.set_setting(config.START_SETTING, start.isoformat())
    config.set_project_start(start)
    clock = {"d": start}
    bot = Bot(db, today=lambda: clock["d"])

    def say(uid, *events):
        for e in events:
            bot.handle(uid, e)

    def tap(data):
        return {"type": "postback", "data": data}

    def text(t):
        return {"type": "text", "text": t}

    def checkin(uid, progress, conf, blocker="no_time", contact=False):
        steps = [text("รายงานความคืบหน้า"), tap(f"s=ci_progress&v={progress}"), tap(f"s=ci_conf&v={conf}")]
        if progress in ("stuck", "not_started"):
            steps += [tap(f"s=ci_blocker&v={blocker}"), tap(f"s=ci_contact&v={'yes' if contact else 'no'}")]
        elif conf <= 2:
            steps += [tap(f"s=ci_contact&v={'yes' if contact else 'no'}")]
        say(uid, *steps)

    def help_request(uid, category, detail=None):
        say(uid, text("ขอความช่วยเหลือ"), tap(f"s=help_cat&v={category}"), text(detail) if detail else tap("s=help_detail&v=skip"))

    for uid, nick, goal, line_name in PEOPLE:
        say(uid, {"type": "follow"}, tap("s=consent&v=yes"), text(nick), tap(f"s=goal&v={goal}"))
        if line_name:
            db.update_user(uid, display_name=line_name)
    say(ONBOARDING_ONLY[0], {"type": "follow"}, tap("s=consent&v=yes"), text(ONBOARDING_ONLY[1]))

    # week 1: nearly everyone reports
    for uid, p, c, extra in [("sim-01", "done", 5, {}), ("sim-02", "doing", 4, {}), ("sim-03", "doing", 3, {}),
                             ("sim-04", "stuck", 2, {"blocker": "dont_know_start", "contact": True}),
                             ("sim-05", "doing", 4, {}), ("sim-06", "not_started", 3, {"blocker": "no_time"}),
                             ("sim-07", "done", 4, {}), ("sim-08", "doing", 4, {}),
                             ("sim-09", "stuck", 1, {"blocker": "tool_hard", "contact": True}), ("sim-10", "doing", 3, {}),
                             ("sim-11", "done", 5, {})]:
        checkin(uid, p, c, **extra)
    help_request("sim-04", "content", "ไม่รู้จะเริ่มเขียน Resume จากตรงไหน")

    # week 2
    clock["d"] = start + timedelta(days=7)
    for uid, p, c, extra in [("sim-01", "done", 5, {}), ("sim-02", "doing", 4, {}),
                             ("sim-03", "stuck", 2, {"blocker": "not_confident", "contact": True}),
                             ("sim-05", "done", 4, {}), ("sim-07", "doing", 4, {}),
                             ("sim-08", "doing", 3, {}), ("sim-09", "stuck", 2, {"blocker": "tool_hard"}),
                             ("sim-10", "not_started", 2, {"blocker": "no_time", "contact": True}), ("sim-11", "done", 5, {})]:
        checkin(uid, p, c, **extra)
    help_request("sim-09", "tool", "ใช้ Canva ไม่เป็น ทำ Portfolio ไม่ได้")
    help_request("sim-03", "confidence", "ไม่มั่นใจว่าพร้อมสมัครงานหรือยัง")

    # week 3 (this week, still open)
    clock["d"] = start + timedelta(days=14)
    for uid, p, c, extra in [("sim-01", "done", 5, {}), ("sim-02", "done", 5, {}), ("sim-05", "done", 5, {}),
                             ("sim-07", "doing", 3, {}), ("sim-11", "done", 4, {}), ("sim-03", "doing", 3, {})]:
        checkin(uid, p, c, **extra)
    help_request("sim-10", "other", "อยากปรึกษาเรื่องเลือกสายงาน")

    # help desk states: #1 answered, #2 accepted, #3 waiting for 3 days (overdue), #4 waiting (new)
    db.update_help(1, status="accepted", changed_by="team")
    db.update_help(1, status="replied", changed_by="team")
    db.update_help(2, status="accepted", changed_by="team")
    db.update_help(3, created_at=(datetime.now().astimezone() - timedelta(days=3)).isoformat())
    print(f"demo data written to {path}: {len(db.all_users())} learners, {len(db.all_checkins())} reports, "
          f"{len(db.list_help())} help requests; project start {start} (today = day 16, week 3)")
    print(f"run it:  DB_PATH={path} python -m backend.app   then open http://127.0.0.1:5000/board")


if __name__ == "__main__":
    main()
