# TIIS check-in bot (backend)

LINE-style chat bot where learners report progress / ask for help, and a triage list tells the team who to follow up first.

## Run
```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 40 tests
python -m backend.app        # http://127.0.0.1:5000  (SQLite file: data.db)
```
Try it without LINE:
```bash
curl -X POST localhost:5000/api/sim/event -H "Content-Type: application/json" \
  -d '{"user_id":"a","type":"follow"}'
curl localhost:5000/api/triage
```

## Connect real LINE (optional)
Set `LINE_CHANNEL_SECRET` and `LINE_CHANNEL_TOKEN`, expose `/webhook` (e.g. with a tunnel) and set it as the channel's webhook URL.
The rich menu's four buttons send these exact texts: `รายงานความคืบหน้า`, `สถานะของฉัน`, `ขอความช่วยเหลือ`, `วิธีใช้งาน/คุยกับทีม`.

## Config (env)
`PROJECT_START` (default 2026-10-19, an assumption), `DB_PATH`, `DEMO_TODAY` (pretend today is a date).

## Known limits
- Triage weights are starting assumptions, untested with real learners.
- Team endpoints have no login (out of scope for the prototype).
- Reply promise "within 1 working day" is a placeholder (`SLA_TEXT`).
