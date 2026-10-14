"""Flask API.
  POST /webhook                     LINE webhook (needs LINE_CHANNEL_SECRET / LINE_CHANNEL_TOKEN)
  POST /api/sim/event               chat simulator: send an event as a user, get the bot replies
  GET  /api/sim/outbox?user_id=..   system-initiated messages waiting for a simulated user
  GET  /board                       team: browser board (no login)
  GET  /api/triage                  team: learners ranked by who to follow up first
  GET  /api/help[?status=..]        team: help requests
  POST /api/help/<id>/status        team: move a request along (accepted/replied/closed)
  POST /api/admin/run-scheduled     queue reminders/follow-ups for a date (demo/cron)
  GET  /api/report[?week=1..6]      team: counts for the board (reported / not reported, progress, help, follow-up)
  GET  /api/export.csv?kind=..      team: learners | checkins | help | followups as CSV
  GET  /api/tasks                   team: weekly tasks shown to learners
  PUT  /api/tasks/<week>            team: set title/description of week 1-4
  GET  /api/followups               team: answers to the 14-day follow-up
There is no login: the board and every team endpoint are open. /api/admin/* (sends reminders) and /api/sim/*
(creates fake learners) are not used by the page and exist only on a local run: on an instance with a LINE token or a
DATABASE_URL they answer 404.
"""
import json
import logging
from datetime import date
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from . import config, line_adapter, reports, scheduler, tasks, team, triage
from .bot import Bot
from .store import open_database


MAX_PUSH_ATTEMPTS = 5


def create_app(db=None, bot=None):
    app = Flask(__name__)
    app.json.ensure_ascii = False
    db = db or open_database()
    bot = bot or Bot(db)

    def flush_outbox():
        """Push queued system messages to real LINE users; simulated users poll instead."""
        if not config.LINE_CHANNEL_TOKEN:
            return
        for row in db.pending_outbox():
            if row["user_id"].startswith("sim-") or row.get("attempts", 0) >= MAX_PUSH_ATTEMPTS:
                continue  # simulated users poll instead; a message that keeps failing stops being retried
            if line_adapter.push(row["user_id"], row["messages"], config.LINE_CHANNEL_TOKEN):
                db.mark_delivered(row["id"])
            else:
                db.record_outbox_failure(row["id"], "LINE push failed")

    @app.before_request
    def hide_internal_endpoints():
        """Reminders and simulated learners must not be reachable by anyone with the link on an instance that can
        message real learners or holds real data."""
        if request.path.startswith(("/api/admin/", "/api/sim/")) and config.restricted_instance():
            return jsonify(error="not available on this instance"), 404
        return None

    @app.get("/board")
    def board():
        resp = send_from_directory(Path(__file__).parent / "static", "board.html")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/health")
    def health():
        return jsonify(ok=True)

    profile_tried = set()  # one attempt per learner per process: a failing lookup must not slow every message

    def remember_profile(user_id):
        u = db.get_user(user_id)
        if not u or u["state"] == "consent" or u["display_name"] or user_id in profile_tried:
            return  # never look up someone who has not consented
        profile_tried.add(user_id)
        name = line_adapter.get_profile(user_id, config.LINE_CHANNEL_TOKEN)
        if name:
            db.update_user(user_id, display_name=name[:100])

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
                if not line_adapter.reply(reply_token, msgs, config.LINE_CHANNEL_TOKEN):
                    # the bot has already moved on, so deliver the same messages by push instead
                    line_adapter.push(user_id, msgs, config.LINE_CHANNEL_TOKEN)
            remember_profile(user_id)  # after replying, so it never delays the answer
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
                         "overdue": reports.is_overdue(r),
                         "nickname": user["nickname"] if user else None,
                         "display_name": user["display_name"] if user else None,
                         "ref": triage.short_ref(r["user_id"])})
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

    @app.get("/api/report")
    def api_report():
        today = config.today()
        raw = request.args.get("week")
        try:
            week = int(raw) if raw else config.week_no(today)
        except ValueError:
            return jsonify(error="week must be a number 1-6"), 400
        if not 1 <= week <= 6:
            return jsonify(error="week must be a number 1-6"), 400
        return jsonify(reports.weekly_report(db, week, today))

    @app.get("/api/export.csv")
    def api_export():
        kind = request.args.get("kind", "")
        if kind not in reports.CSV_KINDS:
            return jsonify(error="kind must be one of " + ", ".join(reports.CSV_KINDS)), 400
        body = reports.export_csv(db, kind, config.today())
        return Response(body, mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=tiis-{kind}.csv",
                                 "Cache-Control": "no-store"})

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
