"""Critical-path tests: delivery to real learners, retries and duplicates, every status transition,
boundary values, the triage decision table, and LINE's message limits."""
import base64
import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from conftest import Chat, register

from backend import config, line_adapter, scheduler, team, triage
from backend.app import create_app

START = config.PROJECT_START


# ============================== delivery to real learners ==================================
class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code, self.text = status_code, text


@pytest.fixture
def line_on(monkeypatch):
    monkeypatch.setattr(config, "LINE_CHANNEL_SECRET", "s3cret")
    monkeypatch.setattr(config, "LINE_CHANNEL_TOKEN", "tok")


def _help_for(bot, db, uid):
    c = register(bot, uid, "เอ")
    c.say("ขอความช่วยเหลือ")
    c.tap("เนื้อหา")
    c.tap("ข้าม")
    return db.list_help()[0]["id"]


def test_status_change_is_pushed_to_a_real_learner_and_marked_delivered(app_client, bot, db, line_on, monkeypatch):
    pushed = []
    monkeypatch.setattr(line_adapter, "push", lambda uid, msgs, tok: pushed.append((uid, msgs, tok)) or True)
    hid = _help_for(bot, db, "Ureal01")
    assert app_client.post(f"/api/help/{hid}/status", json={"status": "accepted"}).status_code == 200
    assert [(u, t) for u, _, t in pushed] == [("Ureal01", "tok")]
    assert "ทีมรับเรื่องแล้ว" in pushed[0][1][0]["text"]
    assert db.pending_outbox("Ureal01") == []


def test_failed_push_stays_queued_and_is_delivered_on_a_later_flush(app_client, bot, db, line_on, monkeypatch):
    calls = []
    monkeypatch.setattr(line_adapter, "push", lambda uid, msgs, tok: calls.append(uid) or len(calls) > 1)  # 1st fails
    hid = _help_for(bot, db, "Ureal01")
    app_client.post(f"/api/help/{hid}/status", json={"status": "accepted"})
    assert len(db.pending_outbox("Ureal01")) == 1          # LINE refused: not lost
    app_client.post(f"/api/help/{hid}/status", json={"status": "replied"})   # the next change flushes the outbox again
    assert calls == ["Ureal01"] * 3                        # the failed one is retried, then the new one
    assert db.pending_outbox("Ureal01") == []


def test_simulated_users_are_never_pushed_to_line(app_client, bot, db, line_on, monkeypatch):
    pushed = []
    monkeypatch.setattr(line_adapter, "push", lambda *a: pushed.append(a) or True)
    hid = _help_for(bot, db, "sim-demo")
    app_client.post(f"/api/help/{hid}/status", json={"status": "accepted"})
    assert pushed == [] and len(db.pending_outbox("sim-demo")) == 1   # waits for the simulator to poll


def test_nothing_is_pushed_when_line_is_not_configured(app_client, bot, db, monkeypatch):
    monkeypatch.setattr(line_adapter, "push", lambda *a: pytest.fail("must not call LINE without a token"))
    hid = _help_for(bot, db, "Ureal01")
    assert app_client.post(f"/api/help/{hid}/status", json={"status": "accepted"}).status_code == 200
    assert len(db.pending_outbox("Ureal01")) == 1


def test_nobody_can_trigger_reminders_or_fake_learners_on_an_instance_that_can_reach_real_learners(
        app_client, bot, db, line_on, monkeypatch):
    monkeypatch.setattr(line_adapter, "push", lambda *a: pytest.fail("a visitor made the server message someone"))
    register(bot, "Ureal01", "เอ")
    assert app_client.post("/api/admin/run-scheduled", json={"today": START.isoformat()}).status_code == 404
    assert app_client.post("/api/sim/event", json={"user_id": "x", "type": "follow"}).status_code == 404
    assert app_client.get("/api/sim/outbox?user_id=x").status_code == 404
    assert db.pending_outbox("Ureal01") == []


@pytest.mark.parametrize("body,status", [
    ({"today": "2026-10-19"}, 200), ({"today": "2026-13-45"}, 400), ({"today": "yesterday"}, 400),
    ({"today": "19/10/2026"}, 400), ({}, 200),
])
def test_run_scheduled_validates_the_date(app_client, body, status):
    assert app_client.post("/api/admin/run-scheduled", json=body).status_code == status


def test_run_scheduled_without_a_date_uses_today(app_client, bot, monkeypatch):
    register(bot, "Ureal01", "เอ")
    monkeypatch.setattr(config, "DEMO_TODAY", START.isoformat())
    assert len(app_client.post("/api/admin/run-scheduled", json={}).get_json()["queued"]) == 1


# ============================== LINE HTTP adapter ==========================================
@pytest.fixture
def http(monkeypatch):
    calls = []
    outcome = {"value": FakeResponse(200)}

    def fake_post(url, json=None, timeout=None, headers=None):
        calls.append({"url": url, "json": json, "timeout": timeout, "headers": headers})
        if isinstance(outcome["value"], Exception):
            raise outcome["value"]
        return outcome["value"]

    monkeypatch.setattr(line_adapter.requests, "post", fake_post)
    return calls, outcome


MSGS = [{"type": "text", "text": "สวัสดี", "quick_replies": [{"label": "ตกลง", "data": "s=a&v=b"}]}]


def test_reply_sends_token_and_converted_messages(http):
    calls, _ = http
    assert line_adapter.reply("rt-1", MSGS, "tok") is True
    call = calls[0]
    assert call["url"].endswith("/message/reply") and call["headers"]["Authorization"] == "Bearer tok"
    assert call["json"]["replyToken"] == "rt-1" and call["timeout"]
    assert call["json"]["messages"][0]["quickReply"]["items"][0]["action"]["data"] == "s=a&v=b"


def test_push_addresses_the_user(http):
    calls, _ = http
    assert line_adapter.push("U1", MSGS, "tok") is True
    assert calls[0]["url"].endswith("/message/push") and calls[0]["json"]["to"] == "U1"


@pytest.mark.parametrize("fn,args", [(line_adapter.reply, ("rt",)), (line_adapter.push, ("U1",))])
def test_empty_message_list_sends_nothing(http, fn, args):
    calls, _ = http
    assert fn(*args, [], "tok") is False and calls == []


@pytest.mark.parametrize("response", [
    FakeResponse(400, '{"message":"Invalid reply token"}'), FakeResponse(401), FakeResponse(429), FakeResponse(500),
    line_adapter.requests.ConnectionError("down"), line_adapter.requests.Timeout("slow"),
])
def test_line_failures_return_false_instead_of_raising(http, response):
    _, outcome = http
    outcome["value"] = response
    assert line_adapter.reply("rt", MSGS, "tok") is False
    assert line_adapter.push("U1", MSGS, "tok") is False


# ============================== webhook: failures, retries, duplicates =====================
def _signed(payload):
    body = json.dumps(payload).encode()
    return body, base64.b64encode(hmac.new(b"s3cret", body, hashlib.sha256).digest()).decode()


def _follow(user="Uw1", event_id="w1", token="rt"):
    return {"events": [{"type": "follow", "source": {"userId": user}, "replyToken": token,
                        "webhookEventId": event_id}]}


def _post(client, payload):
    body, sig = _signed(payload)
    return client.post("/webhook", data=body, headers={"X-Line-Signature": sig})


def test_failed_reply_falls_back_to_push_so_the_learner_still_gets_the_answer(app_client, line_on, monkeypatch):
    pushed = []
    monkeypatch.setattr(line_adapter, "reply", lambda *a: False)       # e.g. expired reply token
    monkeypatch.setattr(line_adapter, "push", lambda uid, msgs, tok: pushed.append((uid, msgs)) or True)
    assert _post(app_client, _follow()).status_code == 200
    assert len(pushed) == 1 and pushed[0][0] == "Uw1" and "ยินยอม" in pushed[0][1][0]["quick_replies"][0]["label"]


def test_successful_reply_does_not_also_push(app_client, line_on, monkeypatch):
    monkeypatch.setattr(line_adapter, "reply", lambda *a: True)
    monkeypatch.setattr(line_adapter, "push", lambda *a: pytest.fail("pushed although the reply worked"))
    assert _post(app_client, _follow()).status_code == 200


def test_reply_and_push_both_failing_still_returns_200(app_client, line_on, monkeypatch):
    monkeypatch.setattr(line_adapter, "reply", lambda *a: False)
    monkeypatch.setattr(line_adapter, "push", lambda *a: False)
    assert _post(app_client, _follow()).status_code == 200


def test_malformed_json_with_a_valid_signature_is_a_400(app_client, line_on):
    body = b"{not json"
    sig = base64.b64encode(hmac.new(b"s3cret", body, hashlib.sha256).digest()).decode()
    assert app_client.post("/webhook", data=body, headers={"X-Line-Signature": sig}).status_code == 400


def test_crash_in_the_bot_gives_a_500_and_the_same_event_works_when_line_redelivers(
        app_client, bot, db, line_on, monkeypatch):
    replies = []
    monkeypatch.setattr(line_adapter, "reply", lambda tok, msgs, t: replies.append(msgs) or True)
    real_handle = bot.handle
    monkeypatch.setattr(bot, "handle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _post(app_client, _follow(event_id="same")).status_code == 500
    monkeypatch.setattr(bot, "handle", real_handle)
    assert _post(app_client, _follow(event_id="same")).status_code == 200      # LINE retry
    assert len(replies) == 1 and db.get_user("Uw1") is not None
    assert _post(app_client, _follow(event_id="same")).status_code == 200      # and a third delivery
    assert len(replies) == 1


def test_event_is_retried_after_a_handler_crash_and_processed_exactly_once(bot, db, monkeypatch):
    register(bot)
    real, calls = bot._handle, {"n": 0}

    def flaky(uid, ev):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(uid, ev)

    monkeypatch.setattr(bot, "_handle", flaky)
    ev = {"type": "text", "text": "ขอความช่วยเหลือ", "event_id": "e-crash"}
    with pytest.raises(RuntimeError):
        bot.handle("U1", ev)
    assert bot.handle("U1", ev) and db.get_user("U1")["state"] == "help_cat"   # retry is processed
    assert bot.handle("U1", ev) == []                                           # but only once


# ============================== help status: every pair =====================================
STATUSES = ["waiting", "accepted", "replied", "closed"]
ALLOWED = {("waiting", "accepted"), ("waiting", "closed"), ("accepted", "replied"),
           ("accepted", "closed"), ("replied", "accepted"), ("replied", "closed")}


@pytest.mark.parametrize("src", STATUSES)
@pytest.mark.parametrize("dst", STATUSES)
def test_every_status_pair_is_accepted_or_rejected_as_specified(bot, db, src, dst):
    register(bot)
    hid = db.create_help("U1", "tool", None)
    db.update_help(hid, status=src)
    ok, info = team.set_help_status(db, hid, dst)
    expected_ok = src == dst or (src, dst) in ALLOWED
    assert ok is expected_ok, f"{src}->{dst}: {ok}/{info}"
    assert db.get_help(hid)["status"] == (dst if (src, dst) in ALLOWED else src)
    notified = len(db.pending_outbox("U1"))
    assert notified == (1 if (src, dst) in ALLOWED else 0), f"{src}->{dst} sent {notified} messages"


# ============================== boundary values ============================================
def _at_nickname(bot):
    c = Chat(bot)
    c.say("สวัสดี")
    c.tap("ยินยอม")
    return c


@pytest.mark.parametrize("nickname,accepted", [
    ("", False), ("   ", False), ("ก", True), ("ก" * 30, True), ("ก" * 31, False),
])
def test_nickname_length_boundaries(bot, db, nickname, accepted):
    c = _at_nickname(bot)
    c.say(nickname)
    assert (db.get_user("U1")["state"] == "goal") is accepted
    assert (db.get_user("U1")["nickname"] == nickname) is accepted


@pytest.mark.parametrize("length,accepted", [(299, True), (300, True), (301, False)])
def test_help_detail_length_boundaries(bot, db, length, accepted):
    c = register(bot)
    c.say("ขอความช่วยเหลือ")
    c.tap("เนื้อหา")
    c.say("ก" * length)
    assert (db.get_user("U1")["state"] == "idle") is accepted
    assert (len(db.list_help()) == 1) is accepted


@pytest.mark.parametrize("length,accepted", [(300, True), (301, False)])
def test_blocker_note_length_boundaries(bot, db, length, accepted):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("ติดอยู่")
    c.tap("2")
    c.tap("อื่นๆ")
    c.say("ก" * length)
    assert (db.get_user("U1")["state"] == "ci_contact") is accepted


def test_whitespace_only_help_detail_is_filed_without_detail(bot, db):
    c = register(bot)
    c.say("ขอความช่วยเหลือ")
    c.tap("เนื้อหา")
    c.say("   ")
    assert db.list_help()[0]["detail"] is None


@pytest.mark.parametrize("value,accepted", [
    ("0", False), ("1", True), ("3", True), ("5", True), ("6", False), ("-1", False), ("x", False),
    ("", False), ("3 ", False), ("๓", False),
])
def test_confidence_accepts_only_whole_numbers_1_to_5(bot, db, value, accepted):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("กำลังทำ")
    c.event({"type": "postback", "data": f"s=ci_conf&v={value}"})
    assert (db.get_user("U1")["state"] != "ci_conf") is accepted


# ============================== invalid input in every state ================================
def _consent(bot):
    c = Chat(bot)
    c.say("สวัสดี")
    return c


def _goal(bot):
    c = _at_nickname(bot)
    c.say("เอ")
    return c


def _ci(bot, *taps):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    for t in taps:
        c.tap(t)
    return c


def _ci_edit(bot):
    c = _ci(bot, "เสร็จแล้ว", "5")
    c.say("รายงานความคืบหน้า")
    return c


def _resume(bot):
    c = _ci(bot, "กำลังทำ")
    c.say("รายงานความคืบหน้า")
    return c


def _help(bot, *taps):
    c = register(bot)
    c.say("ขอความช่วยเหลือ")
    for t in taps:
        c.tap(t)
    return c


STATE_BUILDERS = {
    "consent": _consent, "goal": _goal,
    "ci_progress": lambda b: _ci(b), "ci_conf": lambda b: _ci(b, "กำลังทำ"),
    "ci_blocker": lambda b: _ci(b, "ติดอยู่", "2"), "ci_blocker_text": lambda b: _ci(b, "ติดอยู่", "2", "อื่นๆ"),
    "ci_contact": lambda b: _ci(b, "ติดอยู่", "2", "ไม่มั่นใจ"), "ci_edit": _ci_edit, "resume": _resume,
    "help_cat": lambda b: _help(b), "help_detail": lambda b: _help(b, "เนื้อหา"),
}


@pytest.mark.parametrize("state", sorted(STATE_BUILDERS))
def test_unknown_button_value_keeps_the_state_and_asks_again(bot, db, state):
    c = STATE_BUILDERS[state](bot)
    assert db.get_user("U1")["state"] == state
    c.event({"type": "postback", "data": f"s={state}&v=zzz"})
    assert db.get_user("U1")["state"] == state and "ยังไม่เข้าใจ" in c.text
    saved = 1 if state == "ci_edit" else 0                                  # ci_edit starts with a saved report
    assert len(db.checkin_weeks("U1")) == saved and db.list_help() == []   # nothing saved from bad input


@pytest.mark.parametrize("state", sorted(STATE_BUILDERS))
@pytest.mark.parametrize("data", ["", "garbage", "s=ci_conf", "v=3", "s=&v=", "s=ci_conf&v=", "&&&", "s=%E0%A4%A&v=%"])
def test_malformed_button_data_never_crashes_or_changes_the_state(bot, db, state, data):
    c = STATE_BUILDERS[state](bot)
    c.event({"type": "postback", "data": data})
    assert db.get_user("U1")["state"] == state


# ============================== triage decision table =======================================
def _learner(db, uid="U1", nickname="เอ"):
    db.create_user(uid, START.isoformat(), state="idle")
    db.update_user(uid, nickname=nickname, goal="skill")
    return db.get_user(uid)


# (name, check-in (progress, confidence, blocker, wants_contact) | None, help status | None, days since start,
#  expected score, expected level)
TRIAGE_TABLE = [
    ("stuck only", ("stuck", 3, None, 0), None, 0, 25, "medium"),
    ("not started only", ("not_started", 3, None, 0), None, 0, 15, "normal"),
    ("low confidence while doing: exactly 20 = medium", ("doing", 2, None, 0), None, 0, 20, "medium"),
    ("confidence 3 while doing", ("doing", 3, None, 0), None, 0, 0, "normal"),
    ("done and confident", ("done", 5, None, 0), None, 0, 0, "normal"),
    ("stuck with low confidence", ("stuck", 2, None, 0), None, 0, 45, "high"),
    ("not started + tool too hard", ("not_started", 3, "tool_hard", 0), None, 0, 30, "medium"),
    ("stuck + tool too hard: exactly 40 = high", ("stuck", 3, "tool_hard", 0), None, 0, 40, "high"),
    ("not started + wants contact: exactly 40 = high", ("not_started", 3, None, 1), None, 0, 40, "high"),
    ("only wants contact", ("doing", 4, None, 1), None, 0, 25, "medium"),
    ("everything at once", ("stuck", 1, "tool_hard", 1), None, 0, 85, "high"),
    ("help waiting", ("doing", 4, None, 0), "waiting", 0, 50, "high"),
    ("help accepted but unanswered", None, "accepted", 0, 30, "medium"),
    ("help replied", None, "replied", 0, 0, "normal"),
    ("help closed", None, "closed", 0, 0, "normal"),
    ("missed one finished week", ("doing", 4, None, 0), None, 14, 10, "normal"),   # week 1 sent, week 2 missed
    ("missed two weeks in a row: exactly 40 = high", None, None, 14, 40, "high"),
    ("brand new, nothing yet", None, None, 0, 0, "normal"),
]


@pytest.mark.parametrize("name,checkin,help_status,days,score,level", TRIAGE_TABLE, ids=[r[0] for r in TRIAGE_TABLE])
def test_triage_score_and_level(db, name, checkin, help_status, days, score, level):
    user = _learner(db)
    if checkin:
        progress, conf, blocker, wants = checkin
        db.upsert_checkin("U1", 1, progress, conf, blocker, bool(wants))
    if help_status:
        hid = db.create_help("U1", "tool", None)
        db.update_help(hid, status=help_status)
    row = triage.score_user(db, user, START + timedelta(days=days))
    assert (row["score"], row["level"]) == (score, level), row["reasons"]


def test_triage_breaks_ties_by_nickname_and_skips_learners_without_one(db):
    for uid, nick in (("U1", "ข"), ("U2", "ก")):
        _learner(db, uid, nick)
        db.upsert_checkin(uid, 1, "stuck", 3, None, False)
    db.create_user("U3", START.isoformat(), state="nickname")      # still registering: not listed
    rows = triage.triage(db, START)
    assert [r["nickname"] for r in rows] == ["ก", "ข"]


# ============================== LINE's message limits =======================================
def _walk(bot, db, clock):
    """Drive every flow once and return every reply plus every scheduled push."""
    out = []
    c = Chat(bot)

    def keep():
        out.append(list(c.last))

    c.say("สวัสดี"); keep()
    c.tap("ยินยอม"); keep()
    c.say("เอ"); keep()
    c.tap("ฝึกทักษะ"); keep()
    for text in ("รายงานความคืบหน้า", "สถานะของฉัน", "วิธีใช้งาน/คุยกับทีม", "ขอความช่วยเหลือ"):
        c.say(text); keep()
    c.tap("เนื้อหา"); keep()
    c.say("ช่วยด้วย"); keep()
    c.say("รายงานความคืบหน้า"); keep()
    c.tap("ติดอยู่"); keep()
    c.tap("1"); keep()
    c.tap("อื่นๆ"); keep()
    c.say("ข้อความ"); keep()
    c.tap("ได้เลย"); keep()
    c.say("รายงานความคืบหน้า"); keep()
    c.tap("แก้ไข"); keep()
    c.event({"type": "other"}); keep()
    for due in (START, START + timedelta(days=2), scheduler.FOLLOWUPS["fu_mid"]):
        scheduler.run_scheduled(db, due)
    out.extend(row["messages"] for row in db.pending_outbox("U1"))
    return out


def test_every_message_the_bot_can_send_fits_within_line_limits(bot, db, clock):
    batches = _walk(bot, db, clock)
    assert len(batches) >= 15       # the walk really visited the flows (sanity check)
    for batch in batches:
        assert 0 < len(batch) <= line_adapter.MAX_MESSAGES, [m["text"] for m in batch]
        for m in batch:
            assert 0 < len(m["text"]) <= 5000
            assert len(m.get("quick_replies", [])) <= line_adapter.MAX_QUICK
            for q in m.get("quick_replies", []):
                assert 0 < len(q["label"]) <= line_adapter.MAX_LABEL, q["label"]   # LINE would cut it silently
                assert len(q["data"]) <= 300 and q["data"].startswith("s=")


# ============================== configuration loading =======================================
def test_dotenv_reader_handles_comments_quotes_and_does_not_override_real_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\n\nA_PLAIN=value\nA_QUOTED="with spaces"\nA_EQUALS=a=b=c\nA_EMPTY=\n'
                   'A_EXISTING=from-file\nnot a pair\n', encoding="utf-8")
    for key in ("A_PLAIN", "A_QUOTED", "A_EQUALS", "A_EMPTY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("A_EXISTING", "from-shell")
    config._load_dotenv(env)
    import os
    assert (os.environ["A_PLAIN"], os.environ["A_QUOTED"], os.environ["A_EQUALS"], os.environ["A_EMPTY"]) == (
        "value", "with spaces", "a=b=c", "")
    assert os.environ["A_EXISTING"] == "from-shell"
    config._load_dotenv(tmp_path / "missing.env")      # a missing file is fine


def test_followup_window_still_fits_even_if_the_start_date_moves(monkeypatch):
    # the module asserts this at import time; check the same arithmetic for a few start dates
    from datetime import date
    for start in (date(2026, 10, 19), date(2026, 11, 2), date(2027, 1, 4)):
        main_end = start + timedelta(days=config.MAIN_ACTIVITY_DAYS - 1)
        project_end = start + timedelta(days=config.PROJECT_DAYS - 1)
        assert main_end + timedelta(days=config.FOLLOWUP_MIN_DAYS) <= project_end
