"""LINE Messaging API glue: signature check, event parsing, message conversion, send.
Optional: the bot also runs without LINE through the simulator endpoints in app.py."""
import base64
import hashlib
import hmac
import logging

import requests

log = logging.getLogger("line")
API = "https://api.line.me/v2/bot/message"
MAX_MESSAGES = 5      # LINE: max 5 messages per reply/push call
MAX_QUICK = 13        # LINE: max 13 quick-reply buttons
MAX_LABEL = 20        # LINE: quick-reply label max 20 chars


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def parse_events(payload: dict):
    """Yield (user_id, reply_token, event) in the shape Bot.handle expects."""
    for ev in payload.get("events", []):
        user_id = (ev.get("source") or {}).get("userId")
        if not user_id:
            continue  # e.g. group/room events without a user id: not supported
        etype = ev.get("type")
        if etype == "follow":
            event = {"type": "follow"}
        elif etype == "message":
            msg = ev.get("message") or {}
            event = ({"type": "text", "text": msg.get("text", "")} if msg.get("type") == "text"
                     else {"type": "other"})
        elif etype == "postback":
            event = {"type": "postback", "data": (ev.get("postback") or {}).get("data", "")}
        else:
            continue  # unfollow, join, ...
        event["event_id"] = ev.get("webhookEventId")
        yield user_id, ev.get("replyToken"), event


def to_line(messages):
    out = []
    for m in messages[:MAX_MESSAGES]:
        item = {"type": "text", "text": m["text"][:5000]}
        quick = m.get("quick_replies") or []
        if quick:
            item["quickReply"] = {"items": [{
                "type": "action",
                "action": {"type": "postback", "label": q["label"][:MAX_LABEL],
                           "data": q["data"], "displayText": q["label"][:MAX_LABEL]},
            } for q in quick[:MAX_QUICK]]}
        out.append(item)
    return out


def _post(path, body, token):
    try:
        r = requests.post(f"{API}/{path}", json=body, timeout=10,
                          headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            log.error("LINE %s failed: %s %s", path, r.status_code, r.text[:200])
        return r.status_code == 200
    except requests.RequestException as e:
        log.error("LINE %s error: %s", path, e)
        return False


def reply(reply_token, messages, token):
    return bool(messages) and _post("reply", {"replyToken": reply_token, "messages": to_line(messages)}, token)


def push(user_id, messages, token):
    return bool(messages) and _post("push", {"to": user_id, "messages": to_line(messages)}, token)
