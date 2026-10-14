"""Flask API.
  POST /webhook                     LINE webhook (needs LINE_CHANNEL_SECRET / LINE_CHANNEL_TOKEN)
  POST /api/sim/event               chat simulator: send an event as a user, get the bot replies
  GET  /api/sim/outbox?user_id=..   system-initiated messages waiting for a simulated user
  GET  /board                       team: browser Triage Board (page is public, data needs the token)
  GET  /api/triage                  team: learners ranked by who to follow up first
  GET  /api/help[?status=..]        team: help requests
  POST /api/help/<id>/status        team: move a request along (accepted/replied/closed)
  POST /api/admin/run-scheduled     queue reminders/follow-ups for a date (demo/cron)
  GET  /api/tasks                   team: weekly tasks shown to learners
  PUT  /api/tasks/<week>            team: set title/description of week 1-4
  GET  /api/followups               team: answers to the 14-day follow-up
All /api/* calls need the header X-Team-Token (= TEAM_TOKEN). One shared token, no per-user login."""
import hmac
import json
import logging
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from . import config, line_adapter, scheduler, tasks, team, triage
from .bot import Bot
from .db import Database


def create_app(db=None, bot=None):
    app = Flask(__name__)
    app.json.ensure_ascii = False
    db = db or Database(config.DB_PATH)
    bot = bot or Bot(db)

    def flush_outbox():
        """Push queued system messages to real LINE users; simulated users poll instead."""
        if not config.LINE_CHANNEL_TOKEN:
            return
        for row in db.pending_outbox():
            if row["user_id"].startswith("sim-"):
                continue
            if line_adapter.push(row["user_id"], row["messages"], config.LINE_CHANNEL_TOKEN):
                db.mark_delivered(row["id"])

    @app.before_request
    def require_team_token():
        """/webhook is protected by LINE's signature and /health is public; all of /api/* needs the token."""
        if not request.path.startswith("/api/"):
            return None
        if not config.TEAM_TOKEN:
            return jsonify(error="API disabled: set TEAM_TOKEN"), 503
        given = request.headers.get("X-Team-Token", "")
        if not hmac.compare_digest(given.encode(), config.TEAM_TOKEN.encode()):
            return jsonify(error="missing or wrong X-Team-Token"), 401
        return None

    @app.get("/board")
    def board():
        resp = send_from_directory(Path(__file__).parent / "static", "board.html")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/health")
    def health():
        return jsonify(ok=True)

    @app.post("/webhook")
    def webhook():
        if not config.LINE_CHANNEL_SECRET or not config.LINE_CHANNEL_TOKEN:
            return jsonify(error="LINE is not configured"), 503
        body = request.get_data()
        if not line_adapter.verify_signature(body, request.headers.get("X-Line-Signature", ""),
                                             config.LINE_CHANNEL_SECRET):
            return jsonify(error="bad signature"), 401
        try:
            payload = json.loads(body)
        except ValueError:
            return jsonify(error="invalid json"), 400
        for user_id, reply_token, event in line_adapter.parse_events(payload):
            msgs = bot.handle(user_id, event)
            if msgs and reply_token:
                line_adapter.reply(reply_token, msgs, config.LINE_CHANNEL_TOKEN)
        return jsonify(ok=True)  # always 200 once verified, so LINE does not retry endlessly

    @app.post("/api/sim/event")
    def sim_event():
        body = request.get_json(silent=True) or {}
        user_id = str(body.get("user_id") or "").strip()
        etype = body.get("type")
        if not user_id or etype not in ("follow", "text", "postback", "other"):
            return jsonify(error="user_id and a valid type are required"), 400
        event = {"type": etype, "event_id": body.get("event_id")}
        if etype == "text":
            event["text"] = str(body.get("text", ""))
        if etype == "postback":
            event["data"] = str(body.get("data", ""))
        return jsonify(messages=bot.handle(f"sim-{user_id}" if not user_id.startswith("sim-") else user_id, event))

    @app.get("/api/sim/outbox")
    def sim_outbox():
        user_id = request.args.get("user_id", "")
        if not user_id:
            return jsonify(error="user_id is required"), 400
        uid = user_id if user_id.startswith("sim-") else f"sim-{user_id}"
        rows = db.pending_outbox(uid)
        for r in rows:
            db.mark_delivered(r["id"])
        return jsonify(messages=[m for r in rows for m in r["messages"]])

    @app.get("/api/triage")
    def api_triage():
        rows = triage.triage(db, config.today())
        return jsonify(learners=[{k: v for k, v in r.items() if k != "user_id"} for r in rows])

    @app.get("/api/help")
    def api_help():
        status = request.args.get("status")
        if status and status not in team.TRANSITIONS:
            return jsonify(error="invalid status"), 400
        rows = []
        for r in db.list_help(status):
            user = db.get_user(r["user_id"])
            rows.append({**{k: v for k, v in r.items() if k != "user_id"},
                         "nickname": user["nickname"] if user else None})
        return jsonify(requests=rows)

    @app.post("/api/help/<int:help_id>/status")
    def api_help_status(help_id):
        new = (request.get_json(silent=True) or {}).get("status")
        ok, info = team.set_help_status(db, help_id, new)
        if not ok:
            return jsonify(error=info), 404 if info == "not_found" else 409 if info.startswith("cannot") else 400
        flush_outbox()
        return jsonify(ok=True, result=info, request=db.get_help(help_id))

    @app.post("/api/admin/run-scheduled")
    def api_run_scheduled():
        raw = (request.get_json(silent=True) or {}).get("today")
        try:
            day = date.fromisoformat(raw) if raw else config.today()
        except ValueError:
            return jsonify(error="today must be YYYY-MM-DD"), 400
        queued = scheduler.run_scheduled(db, day)
        flush_outbox()
        return jsonify(queued=[{"user_id": u, "kind": k} for u, k in queued])

    @app.get("/api/tasks")
    def api_tasks():
        return jsonify(tasks=db.list_tasks(), current_week=config.week_no(config.today()))

    @app.put("/api/tasks/<int:week>")
    def api_save_task(week):
        body = request.get_json(silent=True) or {}
        err = tasks.validate_task(week, body.get("title"), body.get("description", ""))
        if err:
            return jsonify(error=err), 404 if err == "week_must_be_1_to_4" else 400
        db.save_task(week, body["title"].strip(), (body.get("description") or "").strip())
        return jsonify(ok=True, task=db.get_task(week))

    @app.get("/api/followups")
    def api_followups():
        return jsonify(followups=db.list_followups())

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(host="127.0.0.1", port=5000)
