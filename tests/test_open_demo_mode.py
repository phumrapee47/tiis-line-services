"""The team board has no login by default (evaluators open it straight away). TEAM_AUTH=on brings it back.
/api/admin/* and /api/sim/* are not used by the page, so they keep the team token on any instance that can
message real learners or holds real data."""
import pytest

from backend import config
from backend.app import create_app

REAL_DB = "postgresql://u:p@h/db"
BOARD_PATHS = ["/api/triage", "/api/help", "/api/tasks", "/api/report", "/api/followups", "/api/export.csv?kind=help"]
INTERNAL_GET = ["/api/sim/outbox?user_id=a"]


@pytest.fixture
def make_client(db, bot):
    def build():
        return create_app(db, bot).test_client()
    return build


def set_env(monkeypatch, *, auth="off", token="team-test-token", line="", database=""):
    monkeypatch.setattr(config, "TEAM_AUTH", auth)
    monkeypatch.setattr(config, "TEAM_TOKEN", token)
    monkeypatch.setattr(config, "LINE_CHANNEL_TOKEN", line)
    monkeypatch.setattr(config, "DATABASE_URL", database)


# ---- the rule: one row per spelling ---------------------------------------------------------------
@pytest.mark.parametrize("value,required", [
    ("on", True), ("ON", True), (" On ", True),
    ("off", False), ("", False), ("yes", False), ("true", False), ("1", False),   # only the word "on" turns it on
])
def test_login_is_required_only_when_team_auth_is_on(monkeypatch, value, required):
    set_env(monkeypatch, auth=value)
    assert config.auth_required() is required


@pytest.mark.parametrize("line,database,restricted", [
    ("", "", False), ("tok", "", True), ("", REAL_DB, True), ("tok", REAL_DB, True),
])
def test_an_instance_is_restricted_when_it_can_reach_real_learners_or_holds_real_data(monkeypatch, line, database,
                                                                                    restricted):
    set_env(monkeypatch, line=line, database=database)
    assert config.restricted_instance() is restricted


# ---- open by default --------------------------------------------------------------------------------
@pytest.mark.parametrize("line,database", [("", ""), ("tok", ""), ("", REAL_DB), ("tok", REAL_DB)])
def test_board_endpoints_need_no_token_by_default_even_on_a_real_instance(make_client, monkeypatch, line, database):
    set_env(monkeypatch, line=line, database=database)
    c = make_client()
    assert [c.get(p).status_code for p in BOARD_PATHS] == [200] * len(BOARD_PATHS)
    assert c.put("/api/tasks/1", json={"title": "ใหม่"}).status_code == 200


def test_board_page_works_without_a_token_header(make_client, monkeypatch):
    set_env(monkeypatch, token="")
    assert make_client().get("/api/triage").status_code == 200       # no TEAM_TOKEN needed at all


@pytest.mark.parametrize("line,database", [("", ""), ("tok", ""), ("", REAL_DB), ("tok", REAL_DB)])
def test_team_auth_on_protects_every_team_endpoint(make_client, monkeypatch, line, database):
    set_env(monkeypatch, auth="on", line=line, database=database)
    c = make_client()
    assert all(c.get(p).status_code == 401 for p in BOARD_PATHS + INTERNAL_GET)
    assert c.put("/api/tasks/1", json={"title": "x"}).status_code == 401
    assert c.get("/api/triage", headers={"X-Team-Token": "team-test-token"}).status_code == 200


def test_team_auth_on_without_a_token_configured_fails_closed(make_client, monkeypatch):
    set_env(monkeypatch, auth="on", token="")
    assert make_client().get("/api/triage").status_code == 503


# ---- admin and simulator stay protected on a real instance -------------------------------------------
INTERNAL_CALLS = [("post", "/api/admin/run-scheduled", {"today": "2026-10-19"}),
                  ("post", "/api/sim/event", {"user_id": "a", "type": "follow"}),
                  ("get", "/api/sim/outbox?user_id=a", None)]


def call(client, method, path, body, headers=None):
    return getattr(client, method)(path, json=body, headers=headers or {}) if body is not None \
        else getattr(client, method)(path, headers=headers or {})


@pytest.mark.parametrize("line,database", [("tok", ""), ("", REAL_DB), ("tok", REAL_DB)])
@pytest.mark.parametrize("method,path,body", INTERNAL_CALLS)
def test_admin_and_simulator_need_the_token_on_a_real_instance_even_with_an_open_board(
        make_client, monkeypatch, line, database, method, path, body):
    set_env(monkeypatch, line=line, database=database)
    c = make_client()
    assert call(c, method, path, body).status_code == 401
    assert call(c, method, path, body, {"X-Team-Token": "wrong"}).status_code == 401
    assert call(c, method, path, body, {"X-Team-Token": "team-test-token"}).status_code == 200


@pytest.mark.parametrize("method,path,body", INTERNAL_CALLS)
def test_admin_and_simulator_are_disabled_on_a_real_instance_with_no_token_configured(
        make_client, monkeypatch, method, path, body):
    set_env(monkeypatch, token="", line="tok")
    assert call(make_client(), method, path, body).status_code == 503      # fail closed


@pytest.mark.parametrize("method,path,body", INTERNAL_CALLS)
def test_admin_and_simulator_are_open_on_a_throwaway_local_run(make_client, monkeypatch, method, path, body):
    set_env(monkeypatch, token="")                     # no LINE, no Postgres: nothing real to reach
    assert call(make_client(), method, path, body).status_code == 200


# ---- session endpoint -------------------------------------------------------------------------------
@pytest.mark.parametrize("kwargs,expected", [
    ({}, False), ({"line": "tok"}, False), ({"database": REAL_DB}, False),
    ({"auth": "on"}, True), ({"auth": "on", "line": "tok", "database": REAL_DB}, True),
])
def test_session_endpoint_is_public_and_says_whether_a_code_is_needed(make_client, monkeypatch, kwargs, expected):
    set_env(monkeypatch, **kwargs)
    r = make_client().get("/api/session")             # no token header
    assert r.status_code == 200 and r.get_json() == {"login_required": expected}


def test_session_endpoint_reveals_nothing_else(make_client, monkeypatch):
    set_env(monkeypatch, auth="on", token="super-secret-code", line="line-secret-token", database=REAL_DB)
    body = make_client().get("/api/session").get_data(as_text=True)
    assert "super-secret-code" not in body and "line-secret-token" not in body and "postgresql" not in body


# ---- warning in the logs ----------------------------------------------------------------------------
def test_open_board_on_a_real_instance_is_logged_as_a_warning(db, bot, monkeypatch, caplog):
    set_env(monkeypatch, line="tok")
    with caplog.at_level("WARNING"):
        create_app(db, bot)
    assert any("team board is OPEN" in r.message for r in caplog.records)


@pytest.mark.parametrize("kwargs", [{}, {"auth": "on", "line": "tok"}, {"auth": "on", "database": REAL_DB}])
def test_no_warning_for_a_local_run_or_when_the_login_is_on(db, bot, monkeypatch, caplog, kwargs):
    set_env(monkeypatch, **kwargs)
    with caplog.at_level("WARNING"):
        create_app(db, bot)
    assert not any("OPEN" in r.message for r in caplog.records)


# ---- the page ---------------------------------------------------------------------------------------
def test_page_asks_the_server_before_showing_a_login_and_tells_visitors_it_is_open(make_client):
    page = make_client().get("/board").get_data(as_text=True)
    assert 'fetch("/api/session"' in page and "login_required" in page
    assert 'id="openNote"' in page and "โหมดเปิดสำหรับการตรวจงาน" in page and "ใครมีลิงก์ก็เปิดดูได้" in page
    assert "signedIn()" in page                       # timers and refresh no longer depend on holding a token
    assert "openMode = false" in page                 # if the server asks for a code later, the login comes back
