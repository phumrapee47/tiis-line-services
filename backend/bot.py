"""Channel-agnostic chat logic: Bot.handle(user_id, event) -> list of messages.

Event shapes (normalised by the channel adapter):
  {"type": "follow"} | {"type": "text", "text": "..."} | {"type": "postback", "data": "s=..&v=.."}
  | {"type": "other"}  (sticker, image, ...), each optionally with "event_id" for de-duplication.
"""
import json
import re
from urllib.parse import parse_qs

from . import messages as M
from .config import today as default_today, week_no

ONBOARD = {"consent", "nickname", "goal"}
CI_STATES = {"ci_progress", "ci_conf", "ci_blocker", "ci_blocker_text", "ci_contact", "ci_edit"}
HELP_STATES = {"help_cat", "help_detail"}
TEXT_STATES = {"nickname", "ci_blocker_text", "help_detail"}
MAX_TEXT = 300

# Rich-menu buttons send these exact texts.
COMMANDS = {
    "รายงานความคืบหน้า": "checkin", "สถานะของฉัน": "status", "ขอความช่วยเหลือ": "help",
    "ช่วยด้วย": "help", "วิธีใช้งาน/คุยกับทีม": "info", "วิธีใช้งาน": "info",
    "วิธีใช้": "info", "คุยกับทีม": "info",
}


def _norm(s):
    return re.sub(r"\s+", "", s or "").lower()


COMMANDS = {_norm(k): v for k, v in COMMANDS.items()}


def parse_data(data):
    return {k: v[0] for k, v in parse_qs(data or "").items()}


class Bot:
    def __init__(self, db, today=default_today):
        self.db = db
        self.today = today

    def handle(self, user_id, event):
        with self.db.lock:
            eid = event.get("event_id")
            if eid and not self.db.mark_event(eid):
                return []  # duplicate delivery (LINE retries): already handled
            try:
                return self._handle(user_id, event)
            except Exception:
                if eid:
                    self.db.unmark_event(eid)  # let a retry succeed
                raise

    # ---- dispatch -------------------------------------------------------------------
    def _handle(self, user_id, event):
        etype = event.get("type")
        user = self.db.get_user(user_id)
        if user is None:
            self.db.create_user(user_id, self.today().isoformat())
            return [M.consent()]
        state = user["state"]
        data = json.loads(user["state_data"] or "{}")

        if etype == "follow":
            if state in ONBOARD:
                return self._prompt(user_id, state, data)
            out = [M.welcome_back(user["nickname"])]
            return out + (self._prompt(user_id, state, data) if state != "idle" else [])

        cmd = None
        if etype == "text":
            cmd = COMMANDS.get(_norm(event.get("text")))
        elif etype == "postback":
            p = parse_data(event.get("data"))
            if p.get("s") == "idle":
                cmd = p.get("v")

        if cmd and state in ONBOARD:
            extra = [M.info()] if cmd in ("info", "help") else []
            return extra + self._prompt(user_id, state, data)
        if cmd:
            return self._command(user, state, data, cmd)

        if etype == "postback":
            p = parse_data(event.get("data"))
            handler = getattr(self, f"_on_{state}", None)
            if p.get("s") != state or handler is None:
                return [M.stale()] + self._prompt(user_id, state, data)
            return handler(user, data, p.get("v"))
        if etype == "text" and state in TEXT_STATES:
            return getattr(self, f"_text_{state}")(user, data, (event.get("text") or "").strip())
        if state == "idle":
            return [M.unknown_idle()]
        return [M.unknown()] + self._prompt(user_id, state, data)

    def _prompt(self, user_id, state, data):
        """Re-ask the question for the current state."""
        week = data.get("week", week_no(self.today()))
        return {
            "consent": lambda: [M.consent()],
            "nickname": lambda: [M.ask_nickname()],
            "goal": lambda: [M.ask_goal()],
            "ci_progress": lambda: [M.ask_progress(week, self.db.get_task(week))],
            "ci_conf": lambda: [M.ask_conf()],
            "ci_blocker": lambda: [M.ask_blocker()],
            "ci_blocker_text": lambda: [M.ask_blocker_text()],
            "ci_contact": lambda: [M.ask_contact()],
            "ci_edit": lambda: [self._confirm_edit(user_id, week)],
            "help_cat": lambda: [M.ask_help_cat()],
            "help_detail": lambda: [M.ask_help_detail()],
            "resume": lambda: [M.resume(data.get("then", "checkin"))],
            "fu": lambda: [M.ask_followup()],
            "idle": lambda: [],
        }[state]()

    def _confirm_edit(self, user_id, week):
        return M.confirm_edit(week, self.db.get_checkin(user_id, week))

    # ---- menu commands --------------------------------------------------------------
    def _command(self, user, state, data, cmd):
        uid = user["user_id"]
        flow = "checkin" if state in CI_STATES else "help" if state in HELP_STATES else None
        if state == "resume":
            flow = data.get("then")

        if cmd in ("status", "info"):  # read-only: answer, then continue where the user was
            out = [self._status(uid)] if cmd == "status" else [M.info()]
            return out + (self._prompt(uid, state, data) if state != "idle" else [])
        if cmd == "checkin":
            if flow == "checkin":
                return self._ask_resume(uid, state, data, "checkin")
            return self._start_checkin(uid, edit=False)
        if cmd == "checkin_edit":
            return self._start_checkin(uid, edit=True)
        if cmd == "help":
            if flow == "help":
                return self._ask_resume(uid, state, data, "help")
            return self._start_help(uid)
        return [M.unknown_idle()]

    def _ask_resume(self, uid, state, data, flow):
        if state != "resume":
            self.db.set_state(uid, "resume", {"prev_state": state, "prev_data": data, "then": flow})
        return [M.resume(flow)]

    def _on_resume(self, user, data, val):
        uid = user["user_id"]
        if val == "continue":
            self.db.set_state(uid, data["prev_state"], data["prev_data"])
            return self._prompt(uid, data["prev_state"], data["prev_data"])
        if val == "restart":
            return self._start_checkin(uid, False) if data.get("then") == "checkin" else self._start_help(uid)
        return [M.unknown()] + self._prompt(uid, "resume", data)

    def _status(self, uid):
        week = week_no(self.today())
        return M.status(week, self.db.get_checkin(uid, week), self.db.user_help(uid, 3),
                        self.db.get_task(week))

    # ---- onboarding -----------------------------------------------------------------
    def _on_consent(self, user, data, val):
        uid = user["user_id"]
        if val == "yes":
            self.db.set_state(uid, "nickname")
            return [M.ask_nickname()]
        if val == "no":
            self.db.delete_user_data(uid)  # nothing kept
            return [M.declined()]
        return [M.unknown(), M.consent()]

    def _text_nickname(self, user, data, t):
        if not t or len(t) > 30:
            return [M.ask_nickname("ชื่อเล่นสั้นๆ ไม่เกิน 30 ตัวอักษรนะ 🙂 ")]
        self.db.update_user(user["user_id"], nickname=t)
        self.db.set_state(user["user_id"], "goal")
        return [M.ask_goal()]

    def _on_goal(self, user, data, val):
        if val not in M.GOALS:
            return [M.unknown(), M.ask_goal()]
        self.db.update_user(user["user_id"], goal=val)
        self.db.set_state(user["user_id"], "idle")
        return [M.onboarded(user["nickname"])]

    # ---- check-in -------------------------------------------------------------------
    def _start_checkin(self, uid, edit):
        week = week_no(self.today())
        existing = self.db.get_checkin(uid, week)
        if existing and not edit:
            self.db.set_state(uid, "ci_edit", {"week": week})
            return [M.confirm_edit(week, existing)]
        self.db.set_state(uid, "ci_progress", {"week": week, "updated": bool(existing)})
        return [M.ask_progress(week, self.db.get_task(week))]

    def _on_ci_edit(self, user, data, val):
        uid = user["user_id"]
        if val == "yes":
            self.db.set_state(uid, "ci_progress", {"week": data["week"], "updated": True})
            return [M.ask_progress(data["week"], self.db.get_task(data["week"]))]
        if val == "no":
            self.db.set_state(uid, "idle")
            return [M.edit_kept()]
        return [M.unknown()] + self._prompt(uid, "ci_edit", data)

    def _on_ci_progress(self, user, data, val):
        if val not in M.PROGRESS:
            return [M.unknown(), M.ask_progress(data["week"], self.db.get_task(data["week"]))]
        self.db.set_state(user["user_id"], "ci_conf", {**data, "progress": val})
        return [M.ask_conf()]

    def _on_ci_conf(self, user, data, val):
        if val not in ("1", "2", "3", "4", "5"):
            return [M.unknown(), M.ask_conf()]
        data = {**data, "confidence": int(val)}
        uid = user["user_id"]
        if data["progress"] in ("not_started", "stuck"):
            self.db.set_state(uid, "ci_blocker", data)
            return [M.ask_blocker()]
        if data["confidence"] <= 2:  # says fine but not confident: offer help anyway
            self.db.set_state(uid, "ci_contact", data)
            return [M.ask_contact()]
        return self._finish_checkin(uid, data)

    def _on_ci_blocker(self, user, data, val):
        uid = user["user_id"]
        if val not in M.BLOCKERS:
            return [M.unknown(), M.ask_blocker()]
        if val == "other":
            self.db.set_state(uid, "ci_blocker_text", data)
            return [M.ask_blocker_text()]
        self.db.set_state(uid, "ci_contact", {**data, "blocker": val})
        return [M.ask_contact()]

    def _text_ci_blocker_text(self, user, data, t):
        if len(t) > MAX_TEXT:
            return [M.text_too_long()]
        self.db.set_state(user["user_id"], "ci_contact", {**data, "blocker": f"other: {t}"})
        return [M.ask_contact()]

    def _on_ci_blocker_text(self, user, data, val):
        if val != "skip":
            return [M.unknown(), M.ask_blocker_text()]
        self.db.set_state(user["user_id"], "ci_contact", {**data, "blocker": "other"})
        return [M.ask_contact()]

    def _on_ci_contact(self, user, data, val):
        if val not in ("yes", "no"):
            return [M.unknown(), M.ask_contact()]
        return self._finish_checkin(user["user_id"], {**data, "wants_contact": val == "yes"})

    def _finish_checkin(self, uid, d):
        wants = bool(d.get("wants_contact", False))
        task = self.db.get_task(d["week"])
        self.db.upsert_checkin(uid, d["week"], d["progress"], d["confidence"], d.get("blocker"), wants,
                               task["title"] if task else None)
        self.db.set_state(uid, "idle")
        return [M.checkin_saved(d["week"], d["progress"], d["confidence"], wants, d.get("updated", False))]

    # ---- help request ---------------------------------------------------------------
    def _start_help(self, uid):
        self.db.set_state(uid, "help_cat")
        return [M.ask_help_cat()]

    def _on_help_cat(self, user, data, val):
        if val not in M.HELP_CATS:
            return [M.unknown(), M.ask_help_cat()]
        self.db.set_state(user["user_id"], "help_detail", {"category": val})
        return [M.ask_help_detail()]

    def _on_help_detail(self, user, data, val):
        if val != "skip":
            return [M.unknown(), M.ask_help_detail()]
        return [self._file_help(user["user_id"], data["category"], None)]

    def _text_help_detail(self, user, data, t):
        if len(t) > MAX_TEXT:
            return [M.text_too_long()]
        return [self._file_help(user["user_id"], data["category"], t or None)]

    def _file_help(self, uid, category, detail):
        """New request, or append to the user's still-open request in the same category."""
        self.db.set_state(uid, "idle")
        open_ = self.db.open_help(uid, category)
        if open_:
            h = open_[0]
            if detail:
                merged = f"{h['detail']} | {detail}" if h["detail"] else detail
                self.db.update_help(h["id"], detail=merged[:2000])
            return M.help_appended(h["id"], h["status"])
        return M.help_created(self.db.create_help(uid, category, detail))

    # ---- 14-day follow-up answer ----------------------------------------------------
    def _on_fu(self, user, data, val):
        uid = user["user_id"]
        if val not in M.FOLLOWUP:
            return [M.unknown(), M.ask_followup()]
        self.db.save_followup(uid, data["kind"], val)
        self.db.set_state(uid, "idle")
        if val == "stuck":
            self._file_help(uid, "other", "[ติดตามผล] ลองนำไปใช้แล้วติดปัญหา")
        return [M.followup_reply(val)]
