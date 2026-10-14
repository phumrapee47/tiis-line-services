"""The team board has no login. The only endpoints kept off the open internet are the ones the page never uses
(/api/admin/*, /api/sim/*), and only on an instance that can message real learners or holds real data."""
import pytest

from backend import config
from backend.app import create_app

REAL_DB = "postgresql://u:p@h/db"
BOARD_PATHS = ["/api/triage", "/api/help", "/api/tasks", "/api/report", "/api/followups", "/api/export.csv?kind=help"]
INTERNAL = [("post", "/api/admin/run-scheduled", {"today": "2026-10-19"}),
            ("post", "/api/sim/event", {"user_id": "a", "type": "follow"}),
            ("get", "/api/sim/outbox?user_id=a", None)]


@pytest.fixture
def make_client(db, bot):
    return lambda: create_app(db, bot).test_client()


def configure(monkeypatch, line="", database=""):
    monkeypatch.setattr(config, "LINE_CHANNEL_TOKEN", line)
    monkeypatch.setattr(config, "DATABASE_URL", database)


def call(client, method, path, body):
    return getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)


@pytest.mark.parametrize("line,database", [("", ""), ("tok", ""), ("", REAL_DB), ("tok", REAL_DB)])
def test_board_endpoints_answer_without_any_credentials_everywhere(make_client, monkeypatch, line, database):
    configure(monkeypatch, line, database)
    c = make_client()
    assert [c.get(p).status_code for p in BOARD_PATHS] == [200] * len(BOARD_PATHS)
    assert c.put("/api/tasks/1", json={"title": "ใหม่"}).status_code == 200
    assert c.get("/board").status_code == 200


def test_there_is_no_session_or_login_endpoint_and_no_token_setting(make_client):
    c = make_client()
    assert c.get("/api/session").status_code == 404
    assert c.post("/api/login", json={}).status_code == 404
    for name in ("TEAM_TOKEN", "TEAM_AUTH", "auth_required", "needs_team_token"):
        assert not hasattr(config, name), name


@pytest.mark.parametrize("method,path,body", INTERNAL)
def test_admin_and_simulator_work_on_a_throwaway_local_run(make_client, monkeypatch, method, path, body):
    configure(monkeypatch)                                  # no LINE token, no Postgres: nothing real to reach
    assert call(make_client(), method, path, body).status_code == 200


@pytest.mark.parametrize("line,database", [("tok", ""), ("", REAL_DB), ("tok", REAL_DB)])
@pytest.mark.parametrize("method,path,body", INTERNAL)
def test_admin_and_simulator_do_not_exist_on_an_instance_with_real_learners_or_data(
        make_client, monkeypatch, line, database, method, path, body):
    configure(monkeypatch, line, database)
    r = call(make_client(), method, path, body)
    assert r.status_code == 404 and r.get_json() == {"error": "not available on this instance"}


@pytest.mark.parametrize("line,database,restricted", [
    ("", "", False), ("tok", "", True), ("", REAL_DB, True), ("tok", REAL_DB, True),
])
def test_an_instance_is_restricted_when_it_can_reach_real_learners_or_holds_real_data(
        monkeypatch, line, database, restricted):
    configure(monkeypatch, line, database)
    assert config.restricted_instance() is restricted


def test_page_has_no_banner_login_or_logout(make_client):
    page = make_client().get("/board").get_data(as_text=True)
    for banned in ("เข้าสู่ระบบ", "ออกจากระบบ", "รหัสทีม", 'type="password"', "โหมดเปิดสำหรับการตรวจงาน", "sessionStorage"):
        assert banned not in page, banned


def test_env_and_deploy_files_no_longer_mention_a_team_token():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for name in (".env.example", "render.yaml", "README.md"):
        text = (root / name).read_text(encoding="utf-8")
        assert "TEAM_TOKEN" not in text and "TEAM_AUTH" not in text, name
