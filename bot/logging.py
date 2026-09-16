"""Structured logging with a hard rule about what never appears in it.

NEVER LOG: message text, agent output, vessel_output.rendered, tool inputs or
results, pairing codes, or any token. For files: never the name, the caption,
the download URL (Telegram's carries the bot token) or a byte of content —
only what kind of thing arrived and its declared MIME type.

Telegram bot chats are not end-to-end encrypted and Telegram stores content —
that is a disclosure the user accepts by using a bot. Vercel's logs would be a
SECOND copy of commercially sensitive fixture and vessel data in a third-party
system, and that one is avoidable. Only whitelisted fields get through.

Thread ids are also excluded: until the ownership work soaks, a thread id is
close to a credential.
"""
from __future__ import annotations

import json
import logging
import sys

_ALLOWED = {
    "event", "env", "region", "commit", "update_id", "chat_id", "user_id",
    "kind", "command", "claim_status", "tools", "iterations", "chunks",
    "duration_ms", "outcome", "error_class", "status", "route", "mime",
    # A 429's wait in seconds: it is what tells a chat sending files faster
    # than 6 a minute from an account at its daily ceiling.
    "retry_after",
}

_logger = logging.getLogger("tropis.bot")
if not _logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(h)
    _logger.setLevel(logging.INFO)

# httpx logs every request line at INFO — full URL included, and every
# Telegram URL (the file download's too) has the bot token in its path. It
# only reaches output if something configures the root logger, which is
# exactly the kind of thing a runtime or a debugging session does quietly.
logging.getLogger("httpx").setLevel(logging.WARNING)


def log(event: str, **fields) -> None:
    payload = {"event": event}
    for k, v in fields.items():
        if k in _ALLOWED:
            payload[k] = v
        else:
            # Loudly visible in review, and impossible to mistake for the value.
            payload[f"{k}__dropped"] = True
    _logger.info(json.dumps(payload, default=str))


def log_exception(event: str, exc: BaseException, **fields) -> None:
    """Only the exception CLASS. Messages can carry paths, SQL fragments or
    API-key material."""
    log(event, error_class=type(exc).__name__, **fields)
