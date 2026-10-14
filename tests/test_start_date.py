"""The project start date is a team setting (stored in the database), and everything dated follows it."""
from datetime import date, timedelta

import pytest
from conftest import register

from backend import config, scheduler
from backend.app import create_app

D = date(2026, 11, 2)       # a Monday: easy to count from


@pytest.fixture(autouse=True)
def restore_start(monkeypatch):
    """PUT /api/settings changes the module-level start date: put it back after every test."""
    monkeypatch.setattr(config, "PROJECT_START", config.PROJECT_START)


@pytest.fixture
def client(db, bot):
    return create_app(db, bot).test_client()


def checkin(bot, uid="U1"):
    c = register(bot, uid, "เอ")
    c.say("รายงานความคืบหน้า")
    c.tap("เสร็จแล้ว")
    c.tap("5")


# ---- the schedule is computed from any start date --------------------------------------------------
@pytest.mark.parametrize("start", [date(2026, 10, 19), date(2026, 12, 31), date(2027, 2, 28), date(2028, 2, 20)])
def test_schedule_always_leaves_at_least_14_days_of_follow_up_inside_the_project(start):
    main_end, end, fu = config.main_activity_end(start), config.project_end(start), config.followup_dates(start)
    assert (main_end - start).days == 27 and (end - start).days == 41            # 4 weeks of activity, 6 weeks in all
    assert fu["fu_mid"] == main_end + timedelta(days=7) and fu["fu_final"] == main_end + timedelta(days=14)
    assert (fu["fu_final"] - main_end).days >= config.FOLLOWUP_MIN_DAYS and fu["fu_final"] <= end


@pytest.mark.parametrize("offset,state", [(-30, "not_started"), (-1, "not_started"), (0, "main"), (27, "main"),
                                          (28, "followup"), (41, "followup"), (42, "ended"), (200, "ended")])
def test_project_state_boundaries(offset, state):
    assert config.project_state(D + timedelta(days=offset), D) == state


def test_old_constants_follow_the_current_start_date(monkeypatch):
    monkeypatch.setattr(config, "PROJECT_START", D)
    assert config.MAIN_ACTIVITY_END == D + timedelta(days=27) and config.PROJECT_END == D + timedelta(days=41)
    assert scheduler.FOLLOWUPS == config.followup_dates(D)
    config.set_project_start(D + timedelta(days=7))
    assert config.PROJECT_END == D + timedelta(days=48) and scheduler.FOLLOWUPS["fu_final"] == D + timedelta(days=48)   # last follow-up = last project day


def test_week_numbers_follow_the_start_date(monkeypatch):
    monkeypatch.setattr(config, "PROJECT_START", D)
    assert [config.week_no(D + timedelta(days=n)) for n in (0, 6, 7, 14, 27, 28, 41)] == [1, 1, 2, 3, 4, 5, 6]
    config.set_project_start(D - timedelta(days=14))
    assert config.week_no(D) == 3


def test_scheduler_fires_on_the_new_start_date_not_the_old_one(bot, db, monkeypatch):
    register(bot, "U1", "เอ")
    old = config.PROJECT_START
    config.set_project_start(old + timedelta(days=3))
    assert scheduler.run_scheduled(db, old) == []                                 # the old first day: nothing
    assert scheduler.run_scheduled(db, old + timedelta(days=3)) == [("U1", "weekly_start")]
    assert scheduler.run_scheduled(db, config.followup_dates()["fu_mid"]) == [("U1", "fu_mid")]


# ---- the API ----------------------------------------------------------------------------------------
def test_settings_say_when_the_project_starts_and_the_schedule(client, monkeypatch):
    monkeypatch.setattr(config, "DEMO_TODAY", (D - timedelta(days=5)).isoformat())
    config.set_project_start(D)
    body = client.get("/api/settings").get_json()
    assert (body["project_start"], body["state"], body["days_until_start"], body["day_no"]) == (D.isoformat(), "not_started", 5, 0)
    assert body["schedule"]["main_end"] == (D + timedelta(days=27)).isoformat()
    assert body["schedule"]["project_end"] == (D + timedelta(days=41)).isoformat()
    assert body["schedule"]["followups"] == {k: v.isoformat() for k, v in config.followup_dates(D).items()}
    assert body["start_is_set"] is False and body["has_reports"] is False


@pytest.mark.parametrize("today_offset,state,day_no,week", [(0, "main", 1, 1), (14, "main", 15, 3), (28, "followup", 29, 5),
                                                            (42, "ended", 43, 6)])
def test_settings_report_where_the_project_is_today(client, monkeypatch, today_offset, state, day_no, week):
    config.set_project_start(D)
    monkeypatch.setattr(config, "DEMO_TODAY", (D + timedelta(days=today_offset)).isoformat())
    body = client.get("/api/settings").get_json()
    assert (body["state"], body["day_no"], body["week"], body["days_until_start"]) == (state, day_no, week, 0)


def test_saving_a_start_date_stores_it_applies_it_and_marks_it_as_set(client, db):
    r = client.put("/api/settings", json={"project_start": "2026-11-09"})
    assert r.status_code == 200 and r.get_json()["project_start"] == "2026-11-09" and r.get_json()["start_is_set"] is True
    assert db.get_project_start() == "2026-11-09" and config.PROJECT_START == date(2026, 11, 9)
    assert client.get("/api/settings").get_json()["start_is_set"] is True


@pytest.mark.parametrize("body", [
    {}, {"project_start": ""}, {"project_start": None}, {"project_start": 20261109}, {"project_start": "2026-13-01"},
    {"project_start": "19/10/2026"}, {"project_start": "2026-02-30"}, {"project_start": "yesterday"},
    {"project_start": "2019-12-31"}, {"project_start": "2101-01-01"}, {"project_start": ["2026-11-09"]},
])
def test_bad_start_dates_are_refused_and_change_nothing(client, db, body):
    before = config.PROJECT_START
    assert client.put("/api/settings", json=body).status_code == 400
    assert config.PROJECT_START == before and db.get_project_start() is None


def test_the_first_and_last_allowed_dates_work(client):
    assert client.put("/api/settings", json={"project_start": "2020-01-01"}).status_code == 200
    assert client.put("/api/settings", json={"project_start": "2100-12-31"}).status_code == 200


def test_malformed_json_is_a_400(client):
    assert client.put("/api/settings", data="not json", content_type="application/json").status_code == 400


def test_moving_the_start_after_reports_exist_needs_an_explicit_confirmation(client, bot, db):
    checkin(bot)
    moved = "2026-12-01"
    r = client.put("/api/settings", json={"project_start": moved})
    assert r.status_code == 409 and r.get_json() == {"error": "reports_exist"}
    assert db.get_project_start() is None                                 # nothing changed
    for not_a_yes in ("true", 1, "yes", None, False):
        assert client.put("/api/settings", json={"project_start": moved, "confirm_shift": not_a_yes}).status_code == 409
    assert client.put("/api/settings", json={"project_start": moved, "confirm_shift": True}).status_code == 200
    assert db.get_project_start() == moved and config.PROJECT_START == date(2026, 12, 1)


def test_saving_the_same_date_again_never_needs_confirmation(client, bot):
    checkin(bot)
    assert client.put("/api/settings", json={"project_start": config.PROJECT_START.isoformat()}).status_code == 200


def test_settings_know_whether_reports_exist(client, bot):
    assert client.get("/api/settings").get_json()["has_reports"] is False
    checkin(bot)
    assert client.get("/api/settings").get_json()["has_reports"] is True


# ---- start-up and effects on other endpoints ----------------------------------------------------------
def test_a_saved_start_date_is_loaded_when_the_app_starts(db, bot):
    db.save_project_start("2027-01-11")
    create_app(db, bot)
    assert config.PROJECT_START == date(2027, 1, 11)


def test_a_corrupt_stored_value_is_ignored_with_a_warning(db, bot, caplog):
    before = config.PROJECT_START
    db._x("INSERT INTO project_config (id, start_date, updated_at) VALUES (1, '2026-02-30', 'x')")   # fits the pattern, is no date
    with caplog.at_level("WARNING"):
        create_app(db, bot)
    assert config.PROJECT_START == before and any("not a date" in r.message for r in caplog.records)


def test_the_report_and_tasks_use_the_saved_start_date(client, monkeypatch):
    client.put("/api/settings", json={"project_start": (D - timedelta(days=14)).isoformat()})
    monkeypatch.setattr(config, "DEMO_TODAY", D.isoformat())
    assert client.get("/api/report").get_json()["week"] == 3
    assert client.get("/api/tasks").get_json()["current_week"] == 3


def test_the_bot_follows_the_start_date_for_the_follow_up_weeks(client, bot, monkeypatch):
    c = register(bot, "U1", "เอ")
    client.put("/api/settings", json={"project_start": (D - timedelta(days=30)).isoformat()})   # today is day 31
    monkeypatch.setattr(config, "DEMO_TODAY", D.isoformat())
    bot.today = config.today
    c.say("รายงานความคืบหน้า")
    assert "ช่วงติดตามผล" in c.text


def test_page_has_the_start_date_editor_and_the_status_strip(client):
    page = client.get("/board").get_data(as_text=True)
    for marker in ('id="startBox"', "วันเริ่มโครงการ", 'type: "date"', "ตารางเวลาที่ได้", "confirm_shift", "reports_exist",
                   "โครงการยังไม่เริ่ม", "ตั้งวันเริ่มโครงการ", 'api("/api/settings"'):
        assert marker in page, marker


def test_the_demo_seed_script_builds_a_consistent_demo_and_refuses_to_overwrite(tmp_path, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
    target = tmp_path / "demo.db"
    ok = subprocess.run([sys.executable, str(script), str(target)], capture_output=True, text=True, encoding="utf-8")
    assert ok.returncode == 0, ok.stderr
    from backend.db import Database
    demo = Database(str(target))
    start = date.fromisoformat(demo.get_project_start())
    assert (date.today() - start).days in (14, 15, 16)                     # today is week 3, never a future week
    weeks = {c["week"] for c in demo.all_checkins()}
    assert weeks == {1, 2, 3}
    assert all("<" not in (u["nickname"] or "") for u in demo.all_users())  # nothing odd-looking in the demo names
    again = subprocess.run([sys.executable, str(script), str(target)], capture_output=True, text=True, encoding="utf-8")
    assert again.returncode != 0 and "already exists" in (again.stderr + again.stdout)
