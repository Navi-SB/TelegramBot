"""POST /api/push — VoyageCalc asking the bot to send a message.

Every other endpoint here is INBOUND: a chat platform calls us, we answer. This
one runs the other way, and it is the first thing in the system that can make
the bot speak to a chat it wasn't just spoken to by. A workflow on a schedule
needs it ("every weekday at 08:00, send me yesterday's fixtures"); nothing else
does.

WHY IT HAS ITS OWN SECRET
    BOT_PUSH_SECRET, not BOT_INTERNAL_SECRET. The latter is a loopback
    credential — Vercel signing a call to itself — and it now lives on exactly
    one machine. This one has to live on the VoyageCalc VPS as well, and it
    authorises a different power, so it gets its own key and its own blast
    radius. Both accept a comma-separated list, so either can be rotated
    without downtime.

WHY THE CHAT IS RE-RESOLVED HERE
    The platform's whole security design is that the bot can never name a user:
    it sends (channel, chat_id) and the platform resolves that through its link
    table, so a leaked service token can only act for chats that already paired
    (see bot/api.py's docstring). An endpoint that takes a chat_id and speaks
    would hand that back — unless it checks. So it checks: GET /api/bot/link
    must say the chat is linked and not blocked before anything is sent.

PLAIN TEXT, NEVER MARKDOWN
    A pushed message can carry text a private-link caller supplied. md_to_html
    turns [click here](http://evil) into a real anchor, which would arrive as a
    clickable link from a bot the broker trusts. Everything is escaped, and a
    provenance line built HERE — not by the caller — says which workflow spoke.

DELIVERY CERTAINTY
    ok:true means every chunk was acknowledged. A refusal includes certain:true
    only when NO chunk was sent. A claimed key alone cannot prove delivery.
"""
from __future__ import annotations

import json

import httpx

from bot.asgi import Resp, json_endpoint
from bot.config import load
from bot.logging import log, log_exception
from bot.platform.client import PlatformClient, PlatformUnavailable
from bot.selfinvoke import stale_timestamp, verify
from bot.telegram.api import TelegramClient, TelegramError
from bot.telegram.html import esc, split_html

MAX_TEXT = 20_000


async def handle(req):
    cfg = load()
    if not cfg.push_secret:
        # Closed by default, exactly like the platform's own service-token
        # dependency: deploying this before the secret exists opens nothing.
        return Resp(200, {"ok": False, "reason": "not_configured", "certain": True})
    if not verify(req.headers, req.body, cfg.push_secret):
        # Two different problems wearing one status code otherwise: a key
        # mismatch is a deploy error, a stale timestamp is clock drift between
        # two machines. Say which.
        reason = "stale_timestamp" if stale_timestamp(req.headers) else "bad_signature"
        log("push_rejected", reason=reason)
        return Resp(401, {"ok": False, "error": reason, "certain": True})

    try:
        payload = json.loads(req.body or b"{}")
    except ValueError:
        return Resp(400, {"ok": False, "error": "invalid_json", "certain": True})
    if not isinstance(payload, dict):
        return Resp(400, {"ok": False, "error": "invalid_json", "certain": True})

    channel = str(payload.get("channel") or "telegram")
    chat_id = str(payload.get("chat_id") or "")
    text = str(payload.get("text") or "")
    source = str(payload.get("source") or "")
    key = str(payload.get("idempotency_key") or "")
    if not chat_id or not text.strip():
        return Resp(400, {"ok": False, "error": "chat_id_and_text_required", "certain": True})
    if len(text) > MAX_TEXT:
        return Resp(400, {"ok": False, "error": "message_too_long", "certain": True})
    if not key or len(key) > 512:
        return Resp(400, {"ok": False, "error": "idempotency_key_required", "certain": True})
    if channel != "telegram":
        # WhatsApp's 24-hour window makes an unprompted message a coin flip,
        # and outside it Meta wants an approved template. Until there is one,
        # this endpoint is Telegram's.
        return Resp(200, {"ok": False, "reason": "unsupported_channel", "certain": True})

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as http:
        api = PlatformClient(cfg.api_base, cfg.service_token, http, channel=channel)
        tg = TelegramClient(cfg.bot_token, http)

        try:
            link = await api.get_link(chat_id)
        except PlatformUnavailable as exc:
            log_exception("push_link_check_failed", exc)
            return Resp(200, {"ok": False, "reason": "bridge_unavailable", "certain": True,
                              "detail": "link check failed before sending"})
        if not link.get("linked"):
            return Resp(200, {"ok": False, "reason": "unlinked", "certain": True})
        if link.get("blocked"):
            return Resp(200, {"ok": False, "reason": "blocked", "certain": True})

        # A timeout on the caller's side can fire after we already sent, and
        # its retry would deliver the report twice. Same machinery the webhook
        # dedupe uses — Telegram redelivering an Approve is the same problem.
        if key:
            try:
                if not await api.claim_event(f"push:{key}"):
                    return Resp(200, {"ok": False, "reason": "duplicate_unknown", "certain": False})
            except PlatformUnavailable as exc:
                log_exception("push_claim_failed", exc)
                return Resp(200, {"ok": False, "reason": "bridge_unavailable", "certain": True,
                                  "detail": "claim failed before sending"})

        header = f"🔁 <b>{esc(source)}</b>\n\n" if source else ""
        chunks = split_html(header + esc(text))
        sent_chunks = 0
        try:
            for chunk in chunks:
                # The platform persists rate-limit backoff. No hidden retries
                # may send after its request has timed out.
                await tg.send_safe(chat_id, chunk, retries=0)
                sent_chunks += 1
        except TelegramError as exc:
            blocked = exc.code == 403
            if blocked:
                # Stop sending: every future push to this chat would 403 too.
                try:
                    await api.mark_blocked(chat_id)
                except PlatformUnavailable:
                    pass
            log("push_failed", code=exc.code, blocked=blocked)
            reason = "blocked" if blocked else "rate_limited" if exc.code == 429 else "refused"
            return Resp(200, {"ok": False, "reason": reason,
                              "certain": sent_chunks == 0 and 400 <= exc.code < 500,
                              "sent_chunks": sent_chunks, "detail": exc.description})
        except Exception as exc:  # noqa: BLE001 — we cannot say whether it landed
            log_exception("push_unknown", exc)
            return Resp(502, {"ok": False, "reason": "upstream", "detail": type(exc).__name__})

    log("push_sent", chat_id=chat_id, chunks=len(chunks))
    return Resp(200, {"ok": True, "chunks": len(chunks)})


app = json_endpoint(handle)
