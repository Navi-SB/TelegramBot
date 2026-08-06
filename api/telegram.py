"""POST /api/telegram — the webhook. Its only job is: authenticate, shape-check,
hand off, 200. Target p99 under 600ms.

It makes ZERO calls to Telegram and ZERO calls to the platform. Everything that
could be slow happens in the worker, because Telegram re-delivers an update it
doesn't get a 200 for within ~60s, and repeated timeouts become a retry storm.
"""
from __future__ import annotations

import hmac
import json

from bot.asgi import Resp, json_endpoint
from bot.config import load
from bot.logging import log
from bot.selfinvoke import fire

_ACTIONABLE = {"message", "callback_query", "my_chat_member"}


async def handle(req):
    cfg = load()

    # Authenticity first, constant-time, before parsing anything. No detail in
    # the body — a prober learns nothing from the response.
    presented = req.headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(presented.encode(), cfg.webhook_secret.encode()):
        log("webhook_bad_secret")
        return Resp(401, {"ok": False})

    try:
        update = json.loads(req.body)
        int(update["update_id"])
    except Exception:
        # Malformed: swallow with 200. A 4xx makes Telegram re-deliver the same
        # garbage until it gives up.
        return Resp(200, {"ok": True})

    # Don't burn a worker invocation on edited_message, channel_post, poll
    # answers and the like.
    if not (_ACTIONABLE & update.keys()):
        return Resp(200, {"ok": True})

    if await fire(f"{cfg.self_url}/api/worker", req.body, cfg.internal_secret, cfg.vercel_bypass):
        return Resp(200, {"ok": True})

    # Hand-off failed. Returning 500 makes Telegram's own at-least-once retry
    # our retry queue — paid for by a dedupe check the worker does anyway.
    # A lost hand-off should not mean a silently unanswered user.
    log("handoff_failed", update_id=update.get("update_id"))
    return Resp(500, {"ok": False})


app = json_endpoint(handle, methods=("POST",))
