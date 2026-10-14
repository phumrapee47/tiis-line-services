# TIIS check-in bot (backend)

LINE-style chat bot where learners report progress / ask for help, and a triage list tells the team who to follow up first.

## Run
```bash
pip install -r requirements-dev.txt
python -m pytest -q          # ~320 tests (SQLite); add TEST_DATABASE_URL to also run the DB contract tests on Postgres
python -m backend.app        # http://127.0.0.1:5000  (SQLite file: data.db)
```
Try it without LINE:
```bash
curl -X POST localhost:5000/api/sim/event -H "Content-Type: application/json" \
  -d '{"user_id":"a","type":"follow"}'
curl localhost:5000/api/triage        # or just open http://127.0.0.1:5000/board (no login)
```

## Connect real LINE (optional)
Set `LINE_CHANNEL_SECRET` and `LINE_CHANNEL_TOKEN`, expose `/webhook` (e.g. with a tunnel) and set it as the channel's webhook URL.
The rich menu's four buttons send these exact texts: `รายงานความคืบหน้า`, `สถานะของฉัน`, `ขอความช่วยเหลือ`, `วิธีใช้งาน/คุยกับทีม`.

## Database
- **SQLite** (default): a file at `DB_PATH`. Zero setup, used by the tests and for running locally.
- **Supabase / Postgres**: set `DATABASE_URL` (Session pooler URI, IPv4, works on Render). Schema: [db/postgres_schema.sql](db/postgres_schema.sql)
  (applied once as a Supabase migration; UUID keys, ENUMs, CHECKs, foreign keys, status log, RLS on with no policies so the public
  API exposes nothing). The app connects as the `postgres` role. Both backends implement the same interface (`backend/db.py`,
  `backend/pg.py`) and `tests/test_db_contract.py` checks they behave the same.

## Config (env)
`PROJECT_START` (default 2026-10-19, an assumption), `DB_PATH`, `DATABASE_URL`, `TEST_DATABASE_URL` (tests only, schema `tiis_test`), `DEMO_TODAY` (pretend today is a date).

## Known limits
- Triage weights are starting assumptions, untested with real learners.
- The team board has **no login** by default, so evaluators can open it straight away: anyone with the link can see learner
  data and change help requests. Set `TEAM_AUTH=on` (and `TEAM_TOKEN`) to require the team code again. `/api/admin/*` and
  `/api/sim/*` always need `TEAM_TOKEN` on an instance with a LINE token or `DATABASE_URL`.
- Reply promise "within 1 working day" is a placeholder (`SLA_TEXT`).
