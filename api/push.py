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

HTTP 200 MEANS DECIDED
    200 {"ok": true} sent, 200 {"ok": false, "reason": …} refused for a reason
    the caller can explain to a person. 5xx means we genuinely don't know, and
    the caller must not claim either way.
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
        return Resp(503, {"ok": False, "error": "push_not_configured"})
    if not verify(req.headers, req.body, cfg.push_secret):
        # Two different problems wearing one status code otherwise: a key
        # mismatch is a deploy error, a stale timestamp is clock drift between
        # two machines. Say which.
        reason = "stale_timestamp" if stale_timestamp(req.headers) else "bad_signature"
        log("push_rejected", reason=reason)
        return Resp(401, {"ok": False, "error": reason})

    try:
        payload = json.loads(req.body or b"{}")
    except ValueError:
        return Resp(400, {"ok": False, "error": "invalid_json"})

    channel = str(payload.get("channel") or "telegram")
    chat_id = str(payload.get("chat_id") or "")
    text = str(payload.get("text") or "")[:MAX_TEXT]
    source = str(payload.get("source") or "")
    key = str(payload.get("idempotency_key") or "")
    if not chat_id or not text.strip():
        return Resp(400, {"ok": False, "error": "chat_id_and_text_required"})
    if channel != "telegram":
        # WhatsApp's 24-hour window makes an unprompted message a coin flip,
        # and outside it Meta wants an approved template. Until there is one,
        # this endpoint is Telegram's.
        return Resp(200, {"ok": False, "reason": "unsupported_channel"})

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as http:
        api = PlatformClient(cfg.api_base, cfg.service_token, http, channel=channel)
        tg = TelegramClient(cfg.bot_token, http)

        try:
            link = await api.get_link(chat_id)
        except PlatformUnavailable as exc:
            log_exception("push_link_check_failed", exc)
            return Resp(502, {"ok": False, "reason": "upstream", "detail": "link check failed"})
        if not link.get("linked"):
            return Resp(200, {"ok": False, "reason": "unlinked"})
        if link.get("blocked"):
            return Resp(200, {"ok": False, "reason": "blocked"})

        # A timeout on the caller's side can fire after we already sent, and
        # its retry would deliver the report twice. Same machinery the webhook
        # dedupe uses — Telegram redelivering an Approve is the same problem.
        if key:
            try:
                if not await api.claim_event(f"push:{key}"):
                    return Resp(200, {"ok": True, "duplicate": True})
            except PlatformUnavailable as exc:
                log_exception("push_claim_failed", exc)
                return Resp(502, {"ok": False, "reason": "upstream", "detail": "claim failed"})

        header = f"🔁 <b>{esc(source)}</b>\n\n" if source else ""
        chunks = split_html(header + esc(text))
        try:
            for chunk in chunks:
                await tg.send_safe(chat_id, chunk)
        except TelegramError as exc:
            blocked = exc.code == 403
            if blocked:
                # Stop sending: every future push to this chat would 403 too.
                try:
                    await api.mark_blocked(chat_id)
                except PlatformUnavailable:
                    pass
            log("push_failed", code=exc.code, blocked=blocked)
            return Resp(200, {"ok": False, "reason": "blocked" if blocked else "refused",
                              "detail": exc.description})
        except Exception as exc:  # noqa: BLE001 — we cannot say whether it landed
            log_exception("push_unknown", exc)
            return Resp(502, {"ok": False, "reason": "upstream", "detail": type(exc).__name__})

    log("push_sent", chat_id=chat_id, chunks=len(chunks))
    return Resp(200, {"ok": True, "chunks": len(chunks)})


app = json_endpoint(handle)
