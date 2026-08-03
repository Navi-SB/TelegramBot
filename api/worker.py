"""POST /api/worker — the slow path. Runs the turn and sends the reply.

Reached only via a signed self-invoke from the webhook; without that signature
this would be a public endpoint that sends Telegram messages and runs agent
turns for anyone who can guess an update shape.
"""
from __future__ import annotations

import json

import httpx

from bot.asgi import Resp, json_endpoint
from bot.config import load
from bot.dispatch import handle_update
from bot.logging import log, log_exception
from bot.platform.client import PlatformClient, PlatformUnavailable
from bot.selfinvoke import verify
from bot.telegram.api import TelegramClient


async def handle(req):
    cfg = load()
    if not verify(req.headers, req.body, cfg.internal_secret):
        return Resp(401, {"ok": False})

    update = json.loads(req.body)
    update_id = update.get("update_id")

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        tg = TelegramClient(cfg.bot_token, http)
        api = PlatformClient(cfg.api_base, cfg.service_token, http)

        # Dedupe BEFORE doing anything with side effects. Telegram retries for
        # up to 24h, and a redelivered Approve would apply a write twice.
        try:
            if not await api.claim_event(str(update_id)):
                log("duplicate_update", update_id=update_id)
                return Resp(200, {"ok": True, "duplicate": True})
        except PlatformUnavailable as exc:
            log_exception("claim_failed", exc, update_id=update_id)
            return Resp(200, {"ok": True})

        try:
            await handle_update(update, tg, api, turn_timeout=cfg.deadline_seconds)
        except Exception as exc:  # noqa: BLE001 — outermost boundary
            log_exception("worker_failed", exc, update_id=update_id)

    return Resp(200, {"ok": True})


app = json_endpoint(handle, methods=("POST",))
