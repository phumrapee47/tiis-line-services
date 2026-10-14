"""Reminders and the >=14-day follow-up. run_scheduled() is idempotent per (user, kind, key),
so calling it several times a day never double-sends. Messages go to the outbox."""
from datetime import timedelta

from . import config
from . import messages as M
from .config import today as default_today

MAIN_WEEKS = config.MAIN_ACTIVITY_DAYS // 7


def __getattr__(name):  # scheduler.FOLLOWUPS follows the current start date
    if name == "FOLLOWUPS":
        return config.followup_dates()
    raise AttributeError(name)


def run_scheduled(db, today=None):
    """Returns the list of (user_id, kind) pushes queued on `today`."""
    today = today or default_today()
    sent = []
    with db.lock:
        users = [u for u in db.all_users() if u["nickname"] and u["state"] != "consent"]
        for w in range(1, MAIN_WEEKS + 1):
            start = config.PROJECT_START + timedelta(days=7 * (w - 1))
            if today == start:  # first day of the week: everyone
                for u in users:
                    if db.mark_push(u["user_id"], "weekly_start", w):
                        db.add_outbox(u["user_id"], [M.weekly_start(w, db.get_task(w))])
                        sent.append((u["user_id"], "weekly_start"))
            if today == start + timedelta(days=2):  # mid-week: only those who haven't reported
                for u in users:
                    if w not in db.checkin_weeks(u["user_id"]) and db.mark_push(u["user_id"], "nudge", w):
                        db.add_outbox(u["user_id"], [M.nudge(w, db.get_task(w))])
                        sent.append((u["user_id"], "nudge"))
        for kind, due in config.followup_dates().items():
            if due <= today <= config.project_end():  # catch up if the run was missed
                for u in users:
                    # only interrupt a learner who is idle; others get it on the next run
                    if u["state"] == "idle" and db.mark_push(u["user_id"], kind, "1"):
                        db.set_state(u["user_id"], "fu", {"kind": kind})
                        db.add_outbox(u["user_id"], [M.ask_followup()])
                        sent.append((u["user_id"], kind))
    return sent
