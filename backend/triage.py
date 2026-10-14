"""Who should the team follow up first? Transparent rules, no ML (60 users = too little data).

The weights are starting assumptions to be tuned after a real pilot, not measured values."""
from datetime import date

from . import messages as M
from .config import today as default_today, week_no
from .tasks import WEEKS

W_HELP_WAITING = 50
W_HELP_ACCEPTED = 30
W_STUCK = 25
W_NOT_STARTED = 15
W_LOW_CONF = 20          # confidence <= 2
W_TOOL_HARD = 15
W_WANTS_CONTACT = 25
W_MISSED_2 = 40          # going silent is the core problem, so it alone reaches "high"
W_MISSED_1 = 10
HIGH, MEDIUM = 40, 20


def short_ref(user_id):
    """Last 4 characters of the LINE id: tells same-nickname learners apart without exposing the full id."""
    return user_id[-4:].lower()


def missed_weeks(db, user, today):
    """Completed weeks (last 2) in which the user had joined but sent no check-in."""
    cur = week_no(today)
    joined = week_no(date.fromisoformat(user["joined_on"]))
    done = db.checkin_weeks(user["user_id"])
    # only weeks that had a check-in to miss: follow-up weeks 5-6 never count
    return sum(1 for w in range(max(joined, cur - 2), min(cur, max(WEEKS) + 1)) if w not in done)


def score_user(db, user, today):
    uid, score, reasons = user["user_id"], 0, []

    for h in db.open_help(uid):
        if h["status"] == "waiting":
            score += W_HELP_WAITING
            reasons.append(f"ขอความช่วยเหลือ #{h['id']} ({M.HELP_CATS[h['category']]}) ยังรอทีมรับเรื่อง")
        else:
            score += W_HELP_ACCEPTED
            reasons.append(f"เรื่อง #{h['id']} ทีมรับแล้วแต่ยังไม่ตอบ")

    c = db.latest_checkin(uid)
    if c:
        if c["progress"] == "stuck":
            score += W_STUCK
            reasons.append("รายงานว่าติดอยู่")
        elif c["progress"] == "not_started":
            score += W_NOT_STARTED
            reasons.append("ยังไม่ได้เริ่ม")
        if c["confidence"] <= 2:
            score += W_LOW_CONF
            note = "" if c["progress"] in ("stuck", "not_started") else f" (แต่บอกว่า{M.PROGRESS[c['progress']][2:]})"
            reasons.append(f"มั่นใจต่ำ ({c['confidence']}/5){note}")
        if c["blocker"] == "tool_hard":
            score += W_TOOL_HARD
            reasons.append("ติดที่เครื่องมือใช้ยาก (ส่งคู่มือสั้นก่อนได้)")
        if c["wants_contact"]:
            score += W_WANTS_CONTACT
            reasons.append("ขอให้ทีมติดต่อ")

    m = missed_weeks(db, user, today)
    if m >= 2:
        score += W_MISSED_2
        reasons.append("ไม่ส่งรายงาน 2 สัปดาห์ติด")
    elif m == 1:
        score += W_MISSED_1
        reasons.append("ไม่ส่งรายงานสัปดาห์ที่แล้ว")

    level = "high" if score >= HIGH else "medium" if score >= MEDIUM else "normal"
    return {"user_id": uid, "nickname": user["nickname"], "display_name": user.get("display_name"),
            "ref": short_ref(uid), "score": score, "level": level, "reasons": reasons or ["ปกติ"], "last_progress": c["progress"] if c else None}


def triage(db, today=None):
    """Registered learners, most urgent first (ties: nickname)."""
    today = today or default_today()
    rows = [score_user(db, u, today) for u in db.all_users() if u["nickname"]]
    return sorted(rows, key=lambda r: (-r["score"], r["nickname"]))
