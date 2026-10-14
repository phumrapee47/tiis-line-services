"""The storage interface must behave the same on SQLite and on Postgres/Supabase.

These tests run on SQLite always, and on Postgres too when TEST_DATABASE_URL is set (throwaway schema
"tiis_test"). Everything else in the suite runs on SQLite only, because Postgres is a network round-trip
away; if the bot only talks to the interface and the interface behaves the same, that is enough."""
from datetime import date, timedelta

import pytest
from conftest import Chat, register

from backend import config, reports, scheduler, triage

START = config.PROJECT_START
ISO_UTC_SUFFIX = "+00:00"


def is_pg(db):
    return type(db).__name__ == "PostgresDatabase"


def add_user(db, uid="U1", nickname="เอ", goal="skill", state="idle", joined=None):
    db.create_user(uid, (joined or START).isoformat(), state=state)
    db.update_user(uid, nickname=nickname, goal=goal)


# ============================== users ======================================================
def test_user_round_trip_with_json_state_and_iso_strings(any_db):
    any_db.create_user("U1", START.isoformat())
    u = any_db.get_user("U1")
    assert (u["user_id"], u["state"], u["nickname"], u["joined_on"]) == ("U1", "consent", None, START.isoformat())
    any_db.set_state("U1", "ci_conf", {"week": 2, "ข้อความ": "ไทย 🎉", "nested": {"a": [1, 2]}})
    import json
    assert json.loads(any_db.get_user("U1")["state_data"]) == {"week": 2, "ข้อความ": "ไทย 🎉", "nested": {"a": [1, 2]}}
    any_db.update_user("U1", nickname="เอ", goal="skill", display_name="Alice ✨",
                       consented_at="2026-10-14T09:00:00+00:00")
    u = any_db.get_user("U1")
    assert (u["nickname"], u["goal"], u["display_name"]) == ("เอ", "skill", "Alice ✨")
    assert u["consented_at"].startswith("2026-10-14T09:00:00")
    assert any_db.get_user("nobody") is None


def test_users_list_is_ordered_and_complete(any_db):
    add_user(any_db, "Ub", "บี", joined=START + timedelta(days=1))
    add_user(any_db, "Ua", "เอ", joined=START)
    add_user(any_db, "Uc", "ซี", joined=START)
    assert [u["user_id"] for u in any_db.all_users()] == ["Ua", "Uc", "Ub"]


def test_nickname_longer_than_30_is_refused_by_the_database(any_db):
    if not is_pg(any_db):
        pytest.skip("only the Postgres schema has this CHECK; the bot enforces it for SQLite")
    any_db.create_user("U1", START.isoformat())
    with pytest.raises(Exception):
        any_db.update_user("U1", nickname="ก" * 31)


def test_deleting_a_user_removes_everything_that_belongs_to_them(any_db):
    add_user(any_db)
    any_db.upsert_checkin("U1", 1, "done", 5, None, False, "งาน")
    hid = any_db.create_help("U1", "tool", "ช่วยด้วย")
    any_db.add_outbox("U1", [{"type": "text", "text": "x", "quick_replies": []}])
    any_db.mark_push("U1", "weekly_start", 1)
    any_db.save_followup("U1", "fu_mid", "used")
    add_user(any_db, "U2", "บี")
    any_db.upsert_checkin("U2", 1, "doing", 3, None, False, "งาน")
    any_db.delete_user_data("U1")
    assert any_db.get_user("U1") is None and any_db.get_help(hid) is None
    assert [c["user_id"] for c in any_db.all_checkins()] == ["U2"]
    assert any_db.pending_outbox("U1") == [] and any_db.count_pushes("weekly_start") == 0
    assert any_db.list_followups() == []


# ============================== webhook de-duplication ==========================================
def test_event_ids_are_accepted_once_until_unmarked(any_db):
    assert any_db.mark_event("e1") is True
    assert any_db.mark_event("e1") is False
    any_db.unmark_event("e1")
    assert any_db.mark_event("e1") is True
    any_db.unmark_event("never-seen")      # harmless


# ============================== check-ins ======================================================
@pytest.mark.parametrize("blocker", [None, "tool_hard", "no_time", "other", "other: ไม่มีคอม",
                                      "other: มี : สอง : colon"])
def test_blocker_round_trips_whether_code_or_free_text(any_db, blocker):
    add_user(any_db)
    any_db.upsert_checkin("U1", 1, "stuck", 2, blocker, True, "หัวข้อ")
    got = any_db.get_checkin("U1", 1)
    assert got["blocker"] == blocker
    assert (got["progress"], got["confidence"], bool(got["wants_contact"]), got["task_title"]) == (
        "stuck", 2, True, "หัวข้อ")


def test_second_report_for_the_same_week_updates_instead_of_adding(any_db):
    add_user(any_db)
    any_db.upsert_checkin("U1", 1, "doing", 3, None, False, "A")
    first = any_db.get_checkin("U1", 1)
    any_db.upsert_checkin("U1", 1, "done", 5, None, True, "B")
    rows = any_db.all_checkins()
    assert len(rows) == 1 and (rows[0]["progress"], rows[0]["task_title"]) == ("done", "B")
    assert rows[0]["created_at"] == first["created_at"]


@pytest.mark.parametrize("confidence", [0, 6, -1])
def test_confidence_outside_1_to_5_is_refused_by_the_database(any_db, confidence):
    add_user(any_db)
    with pytest.raises(Exception):
        any_db.upsert_checkin("U1", 1, "doing", confidence, None, False)


def test_week_outside_the_four_task_weeks_is_refused_by_the_database(any_db):
    if not is_pg(any_db):
        pytest.skip("only the Postgres schema has this foreign key; the bot enforces it for SQLite")
    add_user(any_db)
    with pytest.raises(Exception):
        any_db.upsert_checkin("U1", 5, "doing", 3, None, False)


def test_checkin_queries_order_and_filter(any_db):
    add_user(any_db, "U1", "เอ")
    add_user(any_db, "U2", "บี")
    for uid, week in (("U1", 3), ("U1", 1), ("U2", 2)):
        any_db.upsert_checkin(uid, week, "doing", 3, None, False, f"w{week}")
    assert any_db.checkin_weeks("U1") == {1, 3} and any_db.checkin_weeks("nobody") == set()
    assert any_db.latest_checkin("U1")["week"] == 3 and any_db.latest_checkin("nobody") is None
    assert [(c["week"], c["user_id"]) for c in any_db.all_checkins()] == [(1, "U1"), (2, "U2"), (3, "U1")]


# ============================== help requests ===================================================
def test_help_request_with_and_without_detail(any_db):
    add_user(any_db)
    a = any_db.create_help("U1", "tool", "ใช้ Canva ไม่เป็น")
    b = any_db.create_help("U1", "content", None)
    got_a, got_b = any_db.get_help(a), any_db.get_help(b)
    assert (got_a["user_id"], got_a["category"], got_a["status"], got_a["detail"]) == (
        "U1", "tool", "waiting", "ใช้ Canva ไม่เป็น")
    assert got_b["detail"] is None and b == a + 1
    assert got_a["created_at"].endswith(ISO_UTC_SUFFIX) and got_a["updated_at"].endswith(ISO_UTC_SUFFIX)
    assert any_db.get_help(99999) is None


def test_notes_are_joined_in_order_and_cannot_be_lost(any_db):
    add_user(any_db)
    hid = any_db.create_help("U1", "tool", None)
    any_db.add_help_note(hid, "ข้อแรก")
    any_db.add_help_note(hid, "ข้อสอง 🎉")
    assert any_db.get_help(hid)["detail"] == "ข้อแรก | ข้อสอง 🎉"


def test_open_help_filters_by_status_and_category_and_lists_newest_first(any_db):
    add_user(any_db)
    t = any_db.create_help("U1", "tool", None)
    c = any_db.create_help("U1", "content", None)
    done = any_db.create_help("U1", "confidence", None)
    any_db.update_help(done, status="closed")
    any_db.update_help(c, status="accepted")
    assert [h["id"] for h in any_db.open_help("U1")] == [t, c]
    assert [h["id"] for h in any_db.open_help("U1", "tool")] == [t]
    assert any_db.open_help("U1", "confidence") == []
    assert [h["id"] for h in any_db.user_help("U1", 2)] == [done, c]
    assert [h["id"] for h in any_db.list_help("accepted")] == [c]
    assert [h["id"] for h in any_db.list_help()] == [t, c, done]


def test_status_update_keeps_other_fields_and_bumps_updated_at(any_db):
    add_user(any_db)
    hid = any_db.create_help("U1", "tool", "x")
    before = any_db.get_help(hid)
    any_db.update_help(hid, status="accepted", changed_by="team")
    after = any_db.get_help(hid)
    assert (after["status"], after["detail"], after["created_at"]) == ("accepted", "x", before["created_at"])
    assert after["updated_at"] >= before["updated_at"]


def test_postgres_keeps_a_status_log_and_enforces_one_open_request_per_category(any_db):
    if not is_pg(any_db):
        pytest.skip("Postgres-only guarantees")
    add_user(any_db)
    hid = any_db.create_help("U1", "tool", None)
    any_db.update_help(hid, status="accepted", changed_by="team")
    any_db.update_help(hid, status="accepted", changed_by="team")     # no change: no extra log row
    log = any_db._q("SELECT from_status::text AS f, to_status::text AS t, changed_by FROM help_status_log "
                    "WHERE help_id = %s ORDER BY id", (hid,))
    assert [(r["f"], r["t"], r["changed_by"]) for r in log] == [(None, "waiting", "learner"),
                                                                ("waiting", "accepted", "team")]
    with pytest.raises(Exception):                                     # race-safe: the index refuses a 2nd open one
        any_db.create_help("U1", "tool", None)
    any_db.update_help(hid, status="replied")
    assert any_db.create_help("U1", "tool", None)                      # replied is not "open"


# ============================== outbox / pushes / follow-ups ====================================
def test_outbox_keeps_messages_order_and_delivery_state(any_db):
    add_user(any_db)
    msg = [{"type": "text", "text": "สวัสดี 🎉", "quick_replies": [{"label": "ตกลง", "data": "s=a&v=b"}]}]
    any_db.add_outbox("U1", msg)
    any_db.add_outbox("U1", [{"type": "text", "text": "สอง", "quick_replies": []}])
    rows = any_db.pending_outbox("U1")
    assert [r["messages"][0]["text"] for r in rows] == ["สวัสดี 🎉", "สอง"] and rows[0]["messages"] == msg
    assert rows[0]["attempts"] == 0 and rows[0]["created_at"].endswith(ISO_UTC_SUFFIX)
    any_db.record_outbox_failure(rows[0]["id"], "LINE push failed")
    any_db.record_outbox_failure(rows[0]["id"], "LINE push failed")
    again = any_db.pending_outbox("U1")[0]
    assert (again["attempts"], again["last_error"]) == (2, "LINE push failed")
    any_db.mark_delivered(rows[0]["id"])
    assert [r["messages"][0]["text"] for r in any_db.pending_outbox()] == ["สอง"]
    assert any_db.pending_outbox("nobody") == []


def test_scheduled_pushes_are_recorded_once_and_counted_by_kind(any_db):
    add_user(any_db, "U1")
    add_user(any_db, "U2", "บี")
    assert any_db.mark_push("U1", "weekly_start", 1) is True
    assert any_db.mark_push("U1", "weekly_start", 1) is False
    assert any_db.mark_push("U1", "weekly_start", 2) is True
    assert any_db.mark_push("U2", "weekly_start", 1) is True
    assert any_db.count_pushes("weekly_start") == 3 and any_db.count_pushes("nudge") == 0


def test_followup_answers_overwrite_and_list_in_order(any_db):
    add_user(any_db, "U2", "บี")
    add_user(any_db, "U1", "เอ")
    any_db.save_followup("U1", "fu_mid", "not_yet")
    any_db.save_followup("U1", "fu_mid", "used")
    any_db.save_followup("U2", "fu_final", "stuck")
    assert [(f["user_id"], f["kind"], f["outcome"]) for f in any_db.list_followups()] == [
        ("U1", "fu_mid", "used"), ("U2", "fu_final", "stuck")]


# ============================== weekly tasks ====================================================
def test_default_tasks_are_seeded_and_the_team_can_edit_them(any_db):
    if is_pg(any_db):
        any_db._seed_tasks()
    assert [t["week"] for t in any_db.list_tasks()] == [1, 2, 3, 4]
    any_db.save_task(2, "หัวข้อใหม่", "คำอธิบาย")
    any_db.save_task(2, "หัวข้อใหม่อีกครั้ง", "")
    assert (any_db.get_task(2)["title"], any_db.get_task(2)["description"]) == ("หัวข้อใหม่อีกครั้ง", "")
    assert any_db.get_task(5) is None and len(any_db.list_tasks()) == 4
    with pytest.raises(Exception):
        any_db.save_task(5, "x", "")


# ============================== whole flows on each backend =====================================
def _scenario(any_bot, any_db, clock):
    a = register(any_bot, "U1", "เอ")
    b = register(any_bot, "U2", "บี")
    register(any_bot, "U3", "ซี")
    a.say("รายงานความคืบหน้า")
    a.tap("เสร็จแล้ว")
    a.tap("5")
    b.say("รายงานความคืบหน้า")
    b.tap("ติดอยู่")
    b.tap("1")
    b.tap("เครื่องมือ")
    b.tap("ได้เลย")
    c3 = Chat(any_bot, "U3")
    c3.say("ขอความช่วยเหลือ")
    c3.tap("เครื่องมือ")
    c3.say("ใช้ Canva ไม่เป็น")
    c3.say("ขอความช่วยเหลือ")
    c3.tap("เครื่องมือ")
    c3.say("ลองอีกแล้วยังไม่ได้")            # appended to the same open request
    return a, b, c3


def test_conversation_flow_report_triage_and_csv_agree_on_every_backend(any_bot, any_db, clock):
    _scenario(any_bot, any_db, clock)
    r = reports.weekly_report(any_db, 1, clock.d)
    assert (r["registered"], r["eligible"], r["reported"], len(r["not_reported"])) == (3, 3, 2, 1)
    assert r["progress"] == {"done": 1, "doing": 0, "not_started": 0, "stuck": 1}
    assert r["blockers"] == {"tool_hard": 1} and r["wants_contact"] == 1 and r["avg_confidence"] == 3.0
    assert r["help"]["by_status"]["waiting"] == 1
    rows = triage.triage(any_db, clock.d)
    assert [(x["nickname"], x["level"]) for x in rows] == [("บี", "high"), ("ซี", "high"), ("เอ", "normal")]
    helps = any_db.list_help()
    assert len(helps) == 1 and helps[0]["detail"] == "ใช้ Canva ไม่เป็น | ลองอีกแล้วยังไม่ได้"
    assert reports.export_csv(any_db, "checkins", clock.d).startswith("﻿ref,nickname,week")


def test_consent_is_recorded_and_declining_leaves_nothing(any_bot, any_db):
    c = Chat(any_bot, "U1")
    c.say("สวัสดี")
    c.tap("ยินยอม")
    assert any_db.get_user("U1")["consented_at"].endswith(ISO_UTC_SUFFIX)
    d = Chat(any_bot, "U2")
    d.say("สวัสดี")
    d.tap("ไม่ยินยอม")
    assert any_db.get_user("U2") is None


def test_text_with_a_nul_byte_is_cleaned_not_a_crash(any_bot, any_db):
    c = Chat(any_bot, "U1")
    c.say("สวัสดี")
    c.tap("ยินยอม")
    c.say("เอ\x00บี")
    assert any_db.get_user("U1")["nickname"] == "เอบี"


def test_follow_up_questions_flow_through_the_scheduler(any_bot, any_db, clock):
    a = register(any_bot, "U1", "เอ")
    register(any_bot, "U2", "บี")
    due = scheduler.FOLLOWUPS["fu_mid"]
    assert sorted(scheduler.run_scheduled(any_db, due)) == [("U1", "fu_mid"), ("U2", "fu_mid")]
    assert scheduler.run_scheduled(any_db, due) == []
    a.event({"type": "postback", "data": "s=fu&v=stuck"})
    assert any_db.list_followups()[0]["outcome"] == "stuck"
    assert "[ติดตามผล]" in any_db.list_help()[0]["detail"]
    assert len(any_db.pending_outbox("U2")) == 1


def test_dates_come_back_as_plain_iso_strings(any_db):
    add_user(any_db, joined=date(2026, 10, 19))
    u = any_db.get_user("U1")
    assert u["joined_on"] == "2026-10-19" and isinstance(u["joined_on"], str)


def test_postgres_reconnects_when_the_pooler_drops_the_connection(any_db):
    if not is_pg(any_db):
        pytest.skip("Postgres-only")
    add_user(any_db)
    any_db.conn.close()                    # what an idle-timeout on the pooler looks like to the next query
    assert any_db.get_user("U1")["nickname"] == "เอ"
    any_db.add_help_note(any_db.create_help("U1", "tool", None), "ยังใช้ได้หลังเชื่อมต่อใหม่")


def test_postgres_refuses_unknown_user_and_unknown_fields_instead_of_silently_dropping(any_db):
    if not is_pg(any_db):
        pytest.skip("Postgres-only: SQLite has no foreign keys")
    with pytest.raises(LookupError):
        any_db.create_help("ghost", "tool", None)
    with pytest.raises(LookupError):
        any_db.upsert_checkin("ghost", 1, "doing", 3, None, False)
    add_user(any_db)
    with pytest.raises(ValueError):
        any_db.update_user("U1", state="idle")
    with pytest.raises(ValueError):
        any_db.update_help(1, detail="x")


def test_settings_store_a_value_overwrite_it_and_report_missing_keys(any_db):
    assert any_db.get_setting("project_start") is None
    any_db.set_setting("project_start", "2026-11-02")
    assert any_db.get_setting("project_start") == "2026-11-02"
    any_db.set_setting("project_start", "2026-11-09")
    any_db.set_setting("other", "x")
    assert any_db.get_setting("project_start") == "2026-11-09" and any_db.get_setting("other") == "x"


def test_has_checkins_flips_on_the_first_report(any_db):
    add_user(any_db)
    assert any_db.has_checkins() is False
    any_db.upsert_checkin("U1", 1, "doing", 3, None, False, "งาน")
    assert any_db.has_checkins() is True
