"""The Meta webhook contract, kept importable and testable.

Two verbs on one route:
  GET  — the subscription handshake: check hub.verify_token (constant-time),
         echo hub.challenge back as PLAIN TEXT, byte-for-byte.
  POST — authenticate X-Hub-Signature-256 (HMAC-SHA256 of the RAW body with
         the app secret) before parsing anything, filter out the payloads that
         carry no user message, self-invoke the worker, 200 fast.

The status filter matters: Meta sends sent/delivered/read receipts — three
per outbound message — on the SAME webhook field, and they cannot be
unsubscribed separately. Without the filter every answer we send would wake
three workers for nothing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from ..asgi import Req, Resp
from ..logging import log


def valid_signature(headers: dict[str, str], body: bytes, app_secret: str) -> bool:
    presented = headers.get("x-hub-signature-256", "")
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    # Bytes, not str: compare_digest raises TypeError on non-ASCII str input,
    # which would turn the uniform 401 into a distinguishable 500 for probers.
    return hmac.compare_digest(presented.encode(), expected.encode())


def is_actionable(update: dict[str, Any], phone_number_id: str | None = None) -> bool:
    """True only when some change actually carries inbound messages for OUR
    number — statuses-only payloads (delivery receipts) and traffic addressed
    to other numbers on the same Meta app never wake a worker."""
    if update.get("object") != "whatsapp_business_account":
        return False
    def ours(value: dict[str, Any]) -> bool:
        if not phone_number_id:
            return True
        return (value.get("metadata") or {}).get("phone_number_id") == phone_number_id
    return any(
        c.get("field") == "messages"
        and (c.get("value") or {}).get("messages")
        and ours(c.get("value") or {})
        for e in update.get("entry") or []
        for c in e.get("changes") or []
    )


async def handle_webhook(req: Req, cfg, fire_fn) -> Resp:
    if req.method == "GET":
        token = cfg.wa_verify_token or ""
        if (
            req.query.get("hub.mode") == "subscribe"
            and token
            and hmac.compare_digest(
                req.query.get("hub.verify_token", "").encode(), token.encode())
        ):
            return Resp(200, text=req.query.get("hub.challenge", ""))
        log("wa_webhook_bad_verify")
        return Resp(403, {"ok": False})

    # Authenticity first, constant-time, over the raw bytes. No detail in the
    # body — a prober learns nothing from the response.
    if not (cfg.wa_app_secret and valid_signature(req.headers, req.body, cfg.wa_app_secret)):
        log("wa_webhook_bad_signature")
        return Resp(401, {"ok": False})

    try:
        update = json.loads(req.body)
    except Exception:
        # Malformed: swallow with 200. A 4xx makes Meta re-deliver the same
        # garbage for up to 36 hours.
        return Resp(200, {"ok": True})

    if not is_actionable(update, cfg.wa_phone_number_id):
        return Resp(200, {"ok": True})

    if await fire_fn(f"{cfg.self_url}/api/wa_worker", req.body, cfg.internal_secret,
                     cfg.vercel_bypass):
        return Resp(200, {"ok": True})

    # Hand-off failed. Returning 500 makes Meta's own at-least-once retry our
    # retry queue — paid for by the wamid dedupe the worker does anyway.
    log("wa_handoff_failed")
    return Resp(500, {"ok": False})
