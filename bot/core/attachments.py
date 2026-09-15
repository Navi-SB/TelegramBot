"""Files a user sends — the bridge's whole part in reading them.

The bridge FETCHES and FORWARDS. It never opens a document, never decides
whether something is really a PDF, and never trusts the name or type the
sender's phone attached: VoyageCalc sniffs the bytes and answers with a
readable refusal when they aren't something it can read. Only the channel
holds the token needed to download, so this is the one piece that can't live
there.

What does live here is the size cap, because it is the one check that saves
work BEFORE the work happens. Every number a platform gives us about size is a
hint — Telegram's file_size and Meta's file_size are whatever was declared, and
a Content-Length can be absent or wrong — so the declared size only lets us
refuse early, and the stream itself is what enforces the cap.

Nothing here is logged beyond a short reason code: file names routinely name
owners, charterers and vessels, and a download URL can carry the bot token.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

# Mirrors VoyageCalc's own cap on /api/bot/turn. The backend enforces it too;
# this copy exists so an oversize file is refused without being downloaded.
MAX_BYTES = 5_000_000

_MAX_NAME = 255
_MAX_MIME = 100


@dataclass
class Attachment:
    """A file the user sent, as the channel described it. Nothing downloaded
    yet — an unlinked chat must never cause a download, so the bytes are
    fetched only once the core has decided to run a turn."""

    ref: str                          # Telegram file_id / WhatsApp media id
    file_name: Optional[str] = None
    mime_hint: Optional[str] = None   # sender-declared; the backend sniffs
    size_hint: Optional[int] = None   # declared; enforced again on the stream


class AttachmentError(RuntimeError):
    """str() is a short reason code, safe to log — never a URL or a name."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AttachmentTooLarge(AttachmentError):
    def __init__(self, reason: str = "too_large"):
        super().__init__(reason)


class AttachmentUnavailable(AttachmentError):
    pass


async def read_capped(
    http: httpx.AsyncClient, url: str, max_bytes: int,
    *, headers: Optional[dict[str, str]] = None,
) -> bytes:
    """GET url, streaming, and stop the moment the body passes max_bytes.

    Every failure comes out as one of the two typed errors with a reason
    built here, from a status code or an exception CLASS. The transport's own
    exception is dropped on purpose: httpx errors carry the request, and the
    request's URL can hold the bot token.
    """
    if not url.startswith("https://"):
        # Both platforms hand out https URLs; anything else must not receive
        # a bearer token.
        raise AttachmentUnavailable("not_https")
    try:
        async with http.stream("GET", url, headers=headers, follow_redirects=True) as r:
            if not r.is_success:
                raise AttachmentUnavailable(f"download_{r.status_code}")
            declared = r.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise AttachmentTooLarge()
            buf = bytearray()
            # aiter_bytes, not aiter_raw: a compressed response is capped on
            # what it expands to, which is what the backend would receive.
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise AttachmentTooLarge()
    except AttachmentError:
        raise
    except Exception as exc:  # noqa: BLE001 — the message may hold the URL
        raise AttachmentUnavailable(type(exc).__name__) from None
    if not buf:
        # Neither platform lets anyone send a zero-byte document; an empty
        # body is a download that went wrong, not a file.
        raise AttachmentUnavailable("empty")
    return bytes(buf)


def to_payload(att: Attachment, data: bytes) -> dict[str, Any]:
    """The `attachment` object of POST /api/bot/turn, exactly as agreed with
    VoyageCalc: filename and mime as declared (or null), bytes as standard
    base64. Name and type are trimmed (255 / 100 characters, extension kept)
    so an unusually long name can't turn a readable file into a validation
    error."""
    return {
        "filename": _trim_name(att.file_name),
        "mime_type": (att.mime_hint or "")[:_MAX_MIME] or None,
        "data_b64": base64.b64encode(data).decode("ascii"),
    }


def _trim_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    if len(name) <= _MAX_NAME:
        return name
    stem, ext = os.path.splitext(name)
    if len(ext) > 16:  # not really an extension; keep the start
        return name[:_MAX_NAME]
    return stem[:_MAX_NAME - len(ext)] + ext
