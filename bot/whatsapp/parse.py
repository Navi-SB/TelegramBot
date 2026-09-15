"""WhatsApp webhook payload -> normalized Inbound envelopes.

One POST can batch several messages (and Meta re-delivers for up to 36h), so
this yields a LIST — the worker claims each wamid separately.

Command grammar, since WhatsApp has no /command menu:
  - "LINK <code>" pairs the chat (keyword case-insensitive, code verbatim —
    the wa.me deep link prefills exactly this message)
  - "/agents" style still works for Telegram muscle memory
  - a single bare word (agents / new / status / unlink / help / menu) is a
    command; anything multi-word is a message for the agent — "new fixture
    for MV X" must never be swallowed
"""
from __future__ import annotations

from typing import Any, Optional

from ..core.dispatch import Inbound
from ..logging import log
from .render import log_ref

_BARE_WORDS = {"agents", "new", "status", "unlink", "help", "menu",
               "short", "shortdesc", "shortdescr"}
# "unsupported" is deliberately in MEDIA, not IGNORED: the user actively sent
# something (a poll, view-once media) and must hear that the bot is text-only.
_MEDIA_TYPES = {
    "image", "video", "audio", "document", "sticker", "location", "contacts",
    "unsupported",
}
_IGNORED_TYPES = {"reaction", "system", "ephemeral", "order", "request_welcome"}


def parse_envelopes(
    body: dict[str, Any], phone_number_id: str | None = None
) -> list[Inbound]:
    """phone_number_id: when set, changes addressed to OTHER numbers on the
    same Meta app are skipped — the webhook is per-app, not per-number, so
    without this a test number's traffic would be answered from production."""
    out: list[Inbound] = []
    if body.get("object") != "whatsapp_business_account":
        return out
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if phone_number_id and (
                (value.get("metadata") or {}).get("phone_number_id") != phone_number_id
            ):
                log("wa_foreign_number")
                continue
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in value.get("contacts") or []
            }
            for m in value.get("messages") or []:
                ctx = _parse_message(m, names)
                if ctx:
                    out.append(ctx)
    return out


def _parse_message(m: dict[str, Any], names: dict[str, Optional[str]]) -> Optional[Inbound]:
    mtype = m.get("type")
    frm = str(m.get("from") or "")
    wamid = str(m.get("id") or "")
    if not frm or not wamid or mtype in _IGNORED_TYPES:
        return None

    text: Optional[str] = None
    command: Optional[str] = None
    args = ""
    callback_data: Optional[str] = None
    media = False
    media_mime: Optional[str] = None

    if mtype == "text":
        text = (m.get("text") or {}).get("body") or ""
        if not text.strip():
            # Empty or whitespace-only bodies would ack + claim and then
            # either answer nothing or burn a real agent turn on two spaces.
            return None
        command, args = _parse_command(text)
    elif mtype == "interactive":
        inter = m.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        callback_data = reply.get("id")
        if not callback_data:
            return None  # flow replies etc. — nothing we handle
    elif mtype in _MEDIA_TYPES:
        media = True
        body = m.get(mtype)
        media_mime = body.get("mime_type") if isinstance(body, dict) else None
    else:
        return None

    return Inbound(
        event_id=wamid,
        chat_id=frm,
        sender_id=frm,
        sender_name=names.get(frm),
        text=text,
        command=command,
        args=args,
        callback_data=callback_data,
        callback_id=wamid,
        is_unsupported_media=media,
        is_private=True,  # the Cloud API only delivers 1:1 traffic
        blocked=False,    # WhatsApp has no inbound block signal; learned send-side
        log_ref=log_ref(frm),
        media_kind=mtype if media else None,
        media_mime=media_mime,
    )


def _parse_command(text: str) -> tuple[Optional[str], str]:
    t = text.strip()
    tokens = t.split()
    # The pairing message. Keyword case-insensitive; the code is CASE-SENSITIVE
    # and passed verbatim. A bare code without the keyword is deliberately not
    # attempted — any random first message would trigger a scary failure.
    if len(tokens) == 2 and tokens[0].lower() == "link":
        return "start", tokens[1]
    if t.startswith("/"):
        head, _, rest = t.partition(" ")
        return (head[1:].lower() or None), rest.strip()
    if len(tokens) == 1 and tokens[0].lower() in _BARE_WORDS:
        return tokens[0].lower(), ""
    return None, ""
