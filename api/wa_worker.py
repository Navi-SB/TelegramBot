"""POST /api/wa_worker — the WhatsApp slow path. Runs the turn, sends replies.

Reached only via the signed self-invoke from /api/whatsapp. One webhook POST
can batch several inbound messages; each is claimed (deduped on its wamid)
and handled separately — the loop itself lives in bot/whatsapp/worker.py
where the tests can reach it. Meta retries for up to 36 hours; the claim is
what makes a redelivered Approve harmless.

Log hygiene: raw wamids never appear in logs — a wamid base64-encodes the
sender's phone number, so both chat and event references are keyed hashes.
"""
from __future__ import annotations

import json

import httpx

from bot.asgi import Resp, json_endpoint
from bot.config import load
from bot.logging import log
from bot.platform.client import PlatformClient
from bot.selfinvoke import verify
from bot.whatsapp.api import WhatsAppClient
from bot.whatsapp.channel import WhatsAppChannel
from bot.whatsapp.parse import parse_envelopes
from bot.whatsapp.worker import process_envelopes


async def handle(req):
    cfg = load()
    if not verify(req.headers, req.body, cfg.internal_secret):
        return Resp(401, {"ok": False})
    if not cfg.whatsapp_ready:
        log("wa_worker_unconfigured")
        return Resp(500, {"ok": False, "error": "whatsapp env vars missing"})

    envelopes = parse_envelopes(json.loads(req.body), cfg.wa_phone_number_id)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        wa = WhatsAppClient(cfg.wa_access_token, cfg.wa_phone_number_id, http,
                            cfg.wa_graph_version)
        api = PlatformClient(cfg.api_base, cfg.service_token, http, channel="whatsapp")
        await process_envelopes(
            envelopes, WhatsAppChannel(wa, api), api,
            turn_timeout=cfg.deadline_seconds,
            budget_seconds=float(cfg.max_duration),
        )

    return Resp(200, {"ok": True})


app = json_endpoint(handle, methods=("POST",))
