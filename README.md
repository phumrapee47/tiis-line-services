# TIIS check-in bot (backend)

LINE-style chat bot where learners report progress / ask for help, and a triage list tells the team who to follow up first.

## Run
Python 3.11+. No keys needed to run locally.
```bash
pip install -r requirements-dev.txt
python -m pytest -q            # 566 tests on SQLite
python -m backend.app          # http://127.0.0.1:5000/board  (data.db)
```
Demo board with made-up learners: `python scripts/seed_demo.py demo.db`, then run with `DB_PATH=demo.db`
(PowerShell: `$env:DB_PATH="demo.db"`).

## Keys (`.env`)
`cp .env.example .env` (Windows: `copy`), fill only what you need. It is read on start and git-ignored; real env vars win.

| Key | Source | For |
|---|---|---|
| `LINE_CHANNEL_SECRET`, `LINE_CHANNEL_TOKEN` | LINE Developers > channel (Basic settings / Messaging API) | real LINE |
| `DATABASE_URL` | Supabase > Connect > Session pooler URI | Postgres (empty = SQLite) |
| `TEST_DATABASE_URL` | same kind of URI | optional Postgres tests |

Render: set the three main keys in the dashboard, webhook URL `https://<service>.onrender.com/webhook`.

## Connect real LINE (optional)
Set `LINE_CHANNEL_SECRET` and `LINE_CHANNEL_TOKEN`, expose `/webhook` (e.g. with a tunnel) and set it as the channel's webhook URL.
The rich menu's four buttons send these exact texts: `รายงานความคืบหน้า`, `สถานะของฉัน`, `ขอความช่วยเหลือ`, `วิธีใช้งาน/คุยกับทีม`.

## Database
- **SQLite** (default): a file at `DB_PATH`. Zero setup, used by the tests and for running locally.
- **Supabase / Postgres**: set `DATABASE_URL` (Session pooler URI, IPv4, works on Render). Schema: [db/postgres_schema.sql](db/postgres_schema.sql)
  (applied once as a Supabase migration; UUID keys, ENUMs, CHECKs, foreign keys, status log, RLS on with no policies so the public
  API exposes nothing). The app connects as the `postgres` role. Both backends implement the same interface (`backend/db.py`,
  `backend/pg.py`) and `tests/test_db_contract.py` checks they behave the same.
