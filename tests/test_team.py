"""Triage ranking, help-status workflow, scheduled follow-ups, LINE adapter and HTTP API."""
import base64
import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from conftest import Chat, register

from backend import config, line_adapter, scheduler, team, triage
from backend.app import create_app


def checkin(c, progress, conf, extra=()):
    c.say("รายงานความคืบหน้า")
    c.tap(progress)
    c.tap(str(conf))
    for label in extra:
        c.tap(label)


# ---- triage -----------------------------------------------------------------------
def test_triage_ranks_help_request_then_stuck_then_ok(bot, db, clock):
    ok = register(bot, "U1", "ดี")
    checkin(ok, "เสร็จแล้ว", 5)
    stuck = register(bot, "U2", "ติด")
    checkin(stuck, "ติดอยู่", 2, extra=("ไม่มั่นใจ", "ขอลองเอง"))
    asking = register(bot, "U3", "ขอ")
    asking.say("ขอความช่วยเหลือ")
    asking.tap("เนื้อหา")
    asking.tap("ข้าม")

    rows = triage.triage(db, clock.d)
    assert [r["nickname"] for r in rows] == ["ขอ", "ติด", "ดี"]
    assert [r["level"] for r in rows] == ["high", "high", "normal"]
    assert any("รอทีมรับเรื่อง" in why for why in rows[0]["reasons"])
    assert rows[2]["reasons"] == ["ปกติ"]


def test_triage_flags_low_confidence_hidden_behind_doing(bot, db, clock):
    c = register(bot)
    checkin(c, "กำลังทำ", 2, extra=("ขอลองเอง",))
    row = triage.triage(db, clock.d)[0]
    assert row["level"] == "medium"
    assert any("แต่บอกว่า" in why for why in row["reasons"])


def test_triage_counts_two_missed_weeks(bot, db, clock):
    register(bot)  # joined in week 1, never reports
    clock.d = config.PROJECT_START + timedelta(days=7 * 3)  # week 4: weeks 2 and 3 missed
    row = triage.triage(db, clock.d)[0]
    assert "ไม่ส่งรายงาน 2 สัปดาห์ติด" in row["reasons"] and row["level"] == "high"


def test_new_learner_is_not_flagged_as_missing_in_first_week(bot, db, clock):
    register(bot)
    assert triage.triage(db, clock.d)[0]["reasons"] == ["ปกติ"]


# ---- help workflow ----------------------------------------------------------------
def _one_help(bot):
    c = register(bot)
    c.say("ขอความช่วยเหลือ")
    c.tap("เนื้อหา")
    c.tap("ข้าม")
    return c


def test_help_status_follows_allowed_order_and_notifies_learner(bot, db):
    _one_help(bot)
    assert team.set_help_status(db, 1, "accepted") == (True, "changed")
    assert team.set_help_status(db, 1, "replied") == (True, "changed")
    assert team.set_help_status(db, 1, "closed") == (True, "changed")
    texts = [m["text"] for row in db.pending_outbox("U1") for m in row["messages"]]
    assert any("ทีมรับเรื่องแล้ว" in t for t in texts) and any("ปิดเรื่อง" in t for t in texts)


def test_help_status_rejects_skipping_steps_and_unknown_ids(bot, db):
    _one_help(bot)
    ok, info = team.set_help_status(db, 1, "replied")  # waiting -> replied not allowed
    assert not ok and info.startswith("cannot")
    assert team.set_help_status(db, 999, "accepted") == (False, "not_found")
    assert team.set_help_status(db, 1, "bogus") == (False, "invalid_status")
    assert db.get_help(1)["status"] == "waiting"


def test_help_status_same_value_twice_notifies_once(bot, db):
    _one_help(bot)
    team.set_help_status(db, 1, "accepted")
    assert team.set_help_status(db, 1, "accepted") == (True, "unchanged")
    assert len(db.pending_outbox("U1")) == 1


def test_closed_request_lets_learner_open_a_new_one(bot, db):
    c = _one_help(bot)
    team.set_help_status(db, 1, "closed")
    c.say("ขอความช่วยเหลือ")
    c.tap("เนื้อหา")
    c.tap("ข้าม")
    assert len(db.list_help()) == 2


# ---- scheduler / follow-up ----------------------------------------------------------
def test_followup_window_fits_in_project_and_is_at_least_14_days():
    assert (config.PROJECT_END - config.MAIN_ACTIVITY_END).days >= config.FOLLOWUP_MIN_DAYS
    assert scheduler.FOLLOWUPS["fu_final"] <= config.PROJECT_END


def test_weekly_push_and_nudge_only_for_those_who_have_not_reported(bot, db):
    a = register(bot, "U1", "เอ")
    register(bot, "U2", "บี")
    checkin(a, "เสร็จแล้ว", 5)
    mon = config.PROJECT_START
    assert {k for _, k in scheduler.run_scheduled(db, mon)} == {"weekly_start"}
    sent = scheduler.run_scheduled(db, mon + timedelta(days=2))
    assert sent == [("U2", "nudge")]


def test_scheduler_is_idempotent(bot, db):
    register(bot)
    day = config.PROJECT_START
    assert len(scheduler.run_scheduled(db, day)) == 1
    assert scheduler.run_scheduled(db, day) == []


def test_no_followup_before_main_activity_ends(bot, db):
    register(bot)
    assert scheduler.run_scheduled(db, config.MAIN_ACTIVITY_END) == []


def test_followup_asks_and_stuck_answer_creates_help_request(bot, db):
    c = register(bot)
    due = scheduler.FOLLOWUPS["fu_mid"]
    assert scheduler.run_scheduled(db, due) == [("U1", "fu_mid")]
    c.event({"type": "text", "text": "ทดสอบ"})  # user is now inside the follow-up question
    assert "ยังไม่เข้าใจ" in c.text and "ลองแล้วติดปัญหา" in " ".join(c.buttons)
    c.tap("ติดปัญหา")
    assert db.list_followups()[0]["outcome"] == "stuck"
    assert "[ติดตามผล]" in db.list_help()[0]["detail"]
    assert scheduler.run_scheduled(db, due) == []  # not asked twice


def test_followup_skips_learner_busy_in_a_flow_then_catches_up(bot, db):
    c = register(bot)
    c.say("รายงานความคืบหน้า")  # mid check-in
    due = scheduler.FOLLOWUPS["fu_mid"]
    assert scheduler.run_scheduled(db, due) == []
    c.tap("กำลังทำ")
    c.tap("4")
    assert scheduler.run_scheduled(db, due + timedelta(days=1)) == [("U1", "fu_mid")]


# ---- LINE adapter -----------------------------------------------------------------
def test_signature_check():
    body = b'{"events":[]}'
    good = base64.b64encode(hmac.new(b"s3cret", body, hashlib.sha256).digest()).decode()
    assert line_adapter.verify_signature(body, good, "s3cret")
    assert not line_adapter.verify_signature(body, "bad", "s3cret")
    assert not line_adapter.verify_signature(body, "", "s3cret")
    assert not line_adapter.verify_signature(body, good, "")


def test_parse_events_maps_types_and_skips_unsupported():
    payload = {"events": [
        {"type": "follow", "source": {"userId": "U1"}, "replyToken": "r1", "webhookEventId": "e1"},
        {"type": "message", "source": {"userId": "U1"}, "replyToken": "r2", "webhookEventId": "e2",
         "message": {"type": "text", "text": "สวัสดี"}},
        {"type": "message", "source": {"userId": "U1"}, "replyToken": "r3", "webhookEventId": "e3",
         "message": {"type": "sticker"}},
        {"type": "postback", "source": {"userId": "U1"}, "replyToken": "r4", "webhookEventId": "e4",
         "postback": {"data": "s=a&v=b"}},
        {"type": "unfollow", "source": {"userId": "U1"}},
        {"type": "message", "source": {"type": "group"}, "message": {"type": "text", "text": "x"}},
    ]}
    got = [(e["type"], e["event_id"]) for _, _, e in line_adapter.parse_events(payload)]
    assert got == [("follow", "e1"), ("text", "e2"), ("other", "e3"), ("postback", "e4")]


def test_to_line_enforces_limits():
    msgs = [{"type": "text", "text": "x", "quick_replies": [{"label": "ก" * 40, "data": "d"}] * 20}] * 7
    out = line_adapter.to_line(msgs)
    assert len(out) == 5
    items = out[0]["quickReply"]["items"]
    assert len(items) == 13 and len(items[0]["action"]["label"]) == 20


# ---- HTTP API -----------------------------------------------------------------------
@pytest.fixture
def client(db, bot, monkeypatch):
    monkeypatch.setattr(config, "TEAM_TOKEN", "team-test-token")
    c = create_app(db, bot).test_client()
    c.environ_base["HTTP_X_TEAM_TOKEN"] = "team-test-token"
    return c


def test_api_rejects_missing_or_wrong_token(db, bot, monkeypatch):
    monkeypatch.setattr(config, "TEAM_TOKEN", "team-test-token")
    anon = create_app(db, bot).test_client()
    for path in ("/api/triage", "/api/help", "/api/followups", "/api/sim/outbox?user_id=a"):
        assert anon.get(path).status_code == 401
    assert anon.get("/api/triage", headers={"X-Team-Token": "wrong"}).status_code == 401
    assert anon.post("/api/admin/run-scheduled", json={}).status_code == 401
    assert anon.get("/health").status_code == 200  # public
    assert anon.get("/api/triage", headers={"X-Team-Token": "team-test-token"}).status_code == 200


def test_api_is_disabled_when_no_token_configured(db, bot, monkeypatch):
    monkeypatch.setattr(config, "TEAM_TOKEN", "")
    c = create_app(db, bot).test_client()
    assert c.get("/api/triage").status_code == 503
    assert c.get("/api/triage", headers={"X-Team-Token": ""}).status_code == 503


def sim(client, uid, **kw):
    return client.post("/api/sim/event", json={"user_id": uid, **kw})


def test_sim_endpoint_validates_input(client):
    assert sim(client, "", type="text", text="x").status_code == 400
    assert sim(client, "a", type="weird").status_code == 400
    assert client.post("/api/sim/event", data="not json").status_code == 400


def test_api_end_to_end_help_to_triage_to_status_update(client):
    sim(client, "a", type="follow")
    sim(client, "a", type="postback", data="s=consent&v=yes")
    sim(client, "a", type="text", text="เอ")
    sim(client, "a", type="postback", data="s=goal&v=skill")
    sim(client, "a", type="text", text="ขอความช่วยเหลือ")
    sim(client, "a", type="postback", data="s=help_cat&v=tool")
    sim(client, "a", type="postback", data="s=help_detail&v=skip")
    top = client.get("/api/triage").get_json()["learners"][0]
    assert top["nickname"] == "เอ" and top["level"] == "high"
    r = client.post("/api/help/1/status", json={"status": "accepted"})
    assert r.status_code == 200 and r.get_json()["request"]["status"] == "accepted"
    assert client.post("/api/help/1/status", json={"status": "replied"}).status_code == 200
    assert client.post("/api/help/1/status", json={"status": "waiting"}).status_code == 409
    assert client.post("/api/help/77/status", json={"status": "closed"}).status_code == 404
    out = client.get("/api/sim/outbox?user_id=a").get_json()["messages"]
    assert len(out) == 2 and client.get("/api/sim/outbox?user_id=a").get_json()["messages"] == []


def test_webhook_requires_config_and_valid_signature(client, monkeypatch):
    monkeypatch.setattr(config, "LINE_CHANNEL_SECRET", "")  # tests must not depend on a real .env
    monkeypatch.setattr(config, "LINE_CHANNEL_TOKEN", "")
    assert client.post("/webhook", data=b"{}").status_code == 503
    monkeypatch.setattr(config, "LINE_CHANNEL_SECRET", "s3cret")
    monkeypatch.setattr(config, "LINE_CHANNEL_TOKEN", "tok")
    assert client.post("/webhook", data=b"{}", headers={"X-Line-Signature": "nope"}).status_code == 401

    sent = []
    monkeypatch.setattr(line_adapter, "reply", lambda tok, msgs, t: sent.append((tok, msgs)) or True)
    body = json.dumps({"events": [{"type": "follow", "source": {"userId": "U9"},
                                   "replyToken": "rt", "webhookEventId": "w1"}]}).encode()
    sig = base64.b64encode(hmac.new(b"s3cret", body, hashlib.sha256).digest()).decode()
    assert client.post("/webhook", data=body, headers={"X-Line-Signature": sig}).status_code == 200
    assert sent and sent[0][0] == "rt" and "ยินยอม" in sent[0][1][0]["quick_replies"][0]["label"]
    client.post("/webhook", data=body, headers={"X-Line-Signature": sig})  # LINE retry
    assert len(sent) == 1


def test_board_page_is_served_without_token_and_contains_no_data(db, bot, monkeypatch):
    monkeypatch.setattr(config, "TEAM_TOKEN", "team-test-token")
    register(bot, "U1", "ลับมาก")
    r = create_app(db, bot).test_client().get("/board")  # no token header
    assert r.status_code == 200 and "text/html" in r.content_type
    page = r.get_data(as_text=True)
    assert "Triage Board" in page and "ลับมาก" not in page
    assert r.headers["Cache-Control"] == "no-store"


def test_team_api_shows_nicknames_but_not_line_user_ids(client, bot):
    c = register(bot, "Uabc123secret", "เอ")
    c.say("ขอความช่วยเหลือ")
    c.tap("เนื้อหา")
    c.tap("ข้าม")
    helps = client.get("/api/help").get_json()["requests"]
    learners = client.get("/api/triage").get_json()["learners"]
    assert helps[0]["nickname"] == "เอ" and learners[0]["nickname"] == "เอ"
    assert "Uabc123secret" not in json.dumps([helps, learners])


def test_timestamps_are_utc_with_offset_so_browsers_show_local_time(bot, db):
    c = register(bot)
    c.say("ขอความช่วยเหลือ")
    c.tap("เนื้อหา")
    c.tap("ข้าม")
    assert db.list_help()[0]["created_at"].endswith("+00:00")
