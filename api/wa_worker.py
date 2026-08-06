"""POST /api/wa_worker — the WhatsApp slow path. Runs the turn, sends replies.

Reached only via the signed self-invoke from /api/whatsapp. One webhook POST
can batch several inbound messages; each is claimed (deduped on its wamid)
and handled separately, so a redelivered batch only replays what didn't
finish. Meta retries for up to 36 hours — the claim is what makes a
redelivered Approve harmless.
"""
from __future__ import annotations

import json

import httpx

from bot.asgi import Resp, json_endpoint
from bot.config import load
from bot.core.dispatch import handle_inbound
from bot.logging import log, log_exception
from bot.platform.client import PlatformClient, PlatformUnavailable
from bot.selfinvoke import verify
from bot.whatsapp.api import WhatsAppClient
from bot.whatsapp.channel import WhatsAppChannel
from bot.whatsapp.parse import parse_envelopes


async def handle(req):
    cfg = load()
    if not verify(req.headers, req.body, cfg.internal_secret):
        return Resp(401, {"ok": False})
    if not cfg.whatsapp_ready:
        log("wa_worker_unconfigured")
        return Resp(500, {"ok": False, "error": "whatsapp env vars missing"})

    envelopes = parse_envelopes(json.loads(req.body))

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        wa = WhatsAppClient(cfg.wa_access_token, cfg.wa_phone_number_id, http,
                            cfg.wa_graph_version)
        api = PlatformClient(cfg.api_base, cfg.service_token, http, channel="whatsapp")
        ch = WhatsAppChannel(wa, api)

        for ctx in envelopes:
            # Dedupe BEFORE doing anything with side effects.
            try:
                if not await api.claim_event(ctx.event_id):
                    log("duplicate_update", update_id=ctx.event_id)
                    continue
            except PlatformUnavailable as exc:
                log_exception("claim_failed", exc, update_id=ctx.event_id)
                continue

            try:
                await handle_inbound(ctx, ch, api, turn_timeout=cfg.deadline_seconds)
            except Exception as exc:  # noqa: BLE001 — outermost boundary
                log_exception("worker_failed", exc, update_id=ctx.event_id)

    return Resp(200, {"ok": True})


app = json_endpoint(handle, methods=("POST",))
