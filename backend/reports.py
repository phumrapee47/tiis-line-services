"""Numbers for the team board and CSV export. Everything is computed from stored answers:
nothing here is estimated, and a count is always shown with its denominator."""
import csv
import io
from datetime import date, datetime, timedelta

from .config import THAI_TZ, in_checkin_period, week_no
from .tasks import WEEKS
from .triage import short_ref, triage

FOLLOWUP_KINDS = ("fu_mid", "fu_final")
CSV_KINDS = ("learners", "checkins", "help", "followups")


def _thai(iso):
    return datetime.fromisoformat(iso).astimezone(THAI_TZ)


def help_deadline(created_iso):
    """Promised first reply: one working day after the request (Sat/Sun skipped; Thai public
    holidays are NOT modelled)."""
    due = _thai(created_iso) + timedelta(days=1)
    while due.weekday() >= 5:
        due += timedelta(days=1)
    return due


def is_overdue(help_row, now=None):
    now = now or datetime.now(THAI_TZ)
    return help_row["status"] == "waiting" and now > help_deadline(help_row["created_at"])


def _registered(db):
    return [u for u in db.all_users() if u["nickname"] and u["goal"]]


def weekly_report(db, week, today, now=None):
    """Report for one project week (1-6). `status` says how to read it:
    future = week not started, open = reports still coming in, closed = a finished main-activity
    week, followup = weeks 5-6 (no weekly check-in exists)."""
    current = week_no(today)
    status = ("followup" if week not in WEEKS else "future" if week > current
              else "open" if week == current and in_checkin_period(today) else "closed")

    users = _registered(db)
    eligible = [u for u in users if week_no(date.fromisoformat(u["joined_on"])) <= week]
    by_user = {}
    for c in db.all_checkins():
        by_user.setdefault(c["user_id"], {})[c["week"]] = c

    rows = [(u, by_user.get(u["user_id"], {}).get(week)) for u in eligible]
    reported = [c for _, c in rows if c]
    not_reported = []
    if status in ("open", "closed"):
        for u, c in rows:
            if c:
                continue
            done = by_user.get(u["user_id"], {})
            joined = week_no(date.fromisoformat(u["joined_on"]))
            silent = 0
            last_week = week - 1 if status == "open" else week  # an open week is not missed yet
            for w in range(last_week, joined - 1, -1):  # consecutive weeks without a report
                if w in done:
                    break
                silent += 1
            last = max(done) if done else None
            not_reported.append({"nickname": u["nickname"], "display_name": u.get("display_name"),
                                 "ref": short_ref(u["user_id"]), "silent_weeks": silent,
                                 "last_report_week": last})
        not_reported.sort(key=lambda r: (-r["silent_weeks"], r["nickname"]))

    progress = {k: sum(1 for c in reported if c["progress"] == k)
                for k in ("done", "doing", "not_started", "stuck")}
    blockers = {}
    for c in reported:
        if c["blocker"]:
            key = c["blocker"].split(":")[0]  # "other: free text" -> "other"
            blockers[key] = blockers.get(key, 0) + 1
    conf = [c["confidence"] for c in reported]

    helps = db.list_help()
    now = now or datetime.now(THAI_TZ)
    followups = {}
    answers = db.list_followups()
    for kind in FOLLOWUP_KINDS:
        got = [a for a in answers if a["kind"] == kind]
        asked = db.count_pushes(kind)
        followups[kind] = {"asked": asked, "answered": len(got),
                           "pending": max(asked - len(got), 0),
                           "outcomes": {o: sum(1 for a in got if a["outcome"] == o)
                                        for o in ("used", "not_yet", "stuck")}}

    all_users = db.all_users()
    return {
        "week": week, "current_week": current, "status": status,
        "registered": len(users),
        "onboarding_incomplete": sum(1 for u in all_users if u["state"] in ("nickname", "goal")),
        "awaiting_consent": sum(1 for u in all_users if u["state"] == "consent"),
        "eligible": len(eligible), "reported": len(reported), "not_reported": not_reported,
        "progress": progress, "blockers": blockers,
        "avg_confidence": round(sum(conf) / len(conf), 1) if conf else None,
        "wants_contact": sum(1 for c in reported if c["wants_contact"]),
        "help": {
            "by_status": {s: sum(1 for h in helps if h["status"] == s)
                          for s in ("waiting", "accepted", "replied", "closed")},
            "open_by_category": {k: sum(1 for h in helps if h["category"] == k and h["status"] != "closed")
                                 for k in ("content", "tool", "confidence", "other")},
            "overdue": sum(1 for h in helps if is_overdue(h, now)),
        },
        "followups": followups,
    }


def _safe(value):
    """Spreadsheet formula injection: nicknames and notes are typed by learners."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def export_csv(db, kind, today, now=None):
    """CSV text (UTF-8 with BOM so Excel reads Thai). No LINE ids or LINE names: nickname + short ref only."""
    users = {u["user_id"]: u for u in db.all_users()}

    def who(uid):
        u = users.get(uid) or {}
        return [short_ref(uid), u.get("nickname")]

    out = io.StringIO()
    w = csv.writer(out)
    now = now or datetime.now(THAI_TZ)
    if kind == "learners":
        w.writerow(["ref", "nickname", "goal", "joined_on", "level", "score", "reasons"])
        for r in triage(db, today):
            u = users[r["user_id"]]
            w.writerow([_safe(v) for v in [r["ref"], r["nickname"], u["goal"], u["joined_on"], r["level"],
                                            r["score"], " ; ".join(r["reasons"])]])
    elif kind == "checkins":
        w.writerow(["ref", "nickname", "week", "task_title", "progress", "confidence", "blocker",
                    "wants_contact", "updated_at"])
        for c in db.all_checkins():
            w.writerow([_safe(v) for v in [*who(c["user_id"]), c["week"], c["task_title"], c["progress"],
                                            c["confidence"], c["blocker"], c["wants_contact"], c["updated_at"]]])
    elif kind == "help":
        w.writerow(["id", "ref", "nickname", "category", "status", "overdue", "created_at", "updated_at", "detail"])
        for h in db.list_help():
            w.writerow([_safe(v) for v in [h["id"], *who(h["user_id"]), h["category"], h["status"],
                                            is_overdue(h, now), h["created_at"], h["updated_at"], h["detail"]]])
    elif kind == "followups":
        w.writerow(["ref", "nickname", "kind", "outcome", "created_at"])
        for f in db.list_followups():
            w.writerow([_safe(v) for v in [*who(f["user_id"]), f["kind"], f["outcome"], f["created_at"]]])
    else:
        raise ValueError(kind)
    return "﻿" + out.getvalue()
