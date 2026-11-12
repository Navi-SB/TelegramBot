"""Client for VoyageCalc's /api/bot/* surface.

Note what is NOT here: any way to name a user. Every call carries
(channel, chat_id) and the platform resolves that through its link table. That
is deliberate — it means this token, if it leaks, can only act for chats that
have already paired, rather than impersonating anyone by id.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import unquote

import httpx

CHANNEL = "telegram"  # default; WhatsApp passes channel="whatsapp"

# A report render runs inside an export's approve, so /confirm can take far
# longer than the worker client's 30 s default. Local renders took 0.2-5 s;
# this leaves room for a slow server without outliving the function.
CONFIRM_TIMEOUT = 90.0

# The most a report file may be. A Period Rundown image is under 1 MB and its
# PDF under 20 KB; anything near this is not a file a chat should be sent,
# and reading stops here rather than holding the whole body in memory.
FILE_MAX_BYTES = 20 * 1024 * 1024

_EXT = {"image/png": "png", "application/pdf": "pdf"}
_FILENAME_STAR = re.compile(r"(?:^|;)\s*filename\*\s*=\s*([^;]+)", re.I)
_FILENAME = re.compile(r'(?:^|;)\s*filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]+))', re.I)


class PlatformError(RuntimeError):
    def __init__(self, status: int, detail: Any = None, *, retry_after: Optional[int] = None):
        super().__init__(f"platform {status}")
        self.status = status
        self.detail = detail
        # Seconds, from a 429's Retry-After: how long the user has to wait,
        # which is a minute for the per-chat ceiling and can be hours for the
        # daily one.
        self.retry_after = retry_after


class PlatformUnavailable(PlatformError):
    """The backend could not be reached at all — distinct from it refusing."""


class PlatformClient:
    def __init__(
        self, base: str, service_token: str, client: httpx.AsyncClient,
        channel: str = CHANNEL,
    ):
        self._base = base.rstrip("/")
        self._http = client
        self._channel = channel
        self._headers = {"X-Service-Token": service_token, "X-Bot-Channel": channel}

    async def _call(self, method: str, path: str, **kw) -> Any:
        try:
            r = await self._http.request(
                method, f"{self._base}{path}", headers=self._headers, **kw
            )
        except Exception as exc:
            raise PlatformUnavailable(0, type(exc).__name__) from exc
        if r.status_code >= 400:
            raise _error(r)
        return r.json() if r.content else {}

    # --- dedupe -------------------------------------------------------------

    async def claim_event(self, event_id: str) -> bool:
        """False means this update has already been handled. Telegram retries
        for up to 24h, and a redelivered Approve would apply a write twice."""
        res = await self._call(
            "POST", "/api/bot/events/claim",
            json={"channel": self._channel, "event_id": str(event_id)},
        )
        return bool(res.get("fresh"))

    # --- linking ------------------------------------------------------------

    async def redeem(self, chat_id: str, code: str, tg_user_id: str, name: str | None):
        return await self._call("POST", "/api/bot/link/redeem", json={
            "channel": self._channel, "chat_id": chat_id, "code": code,
            "channel_user_id": tg_user_id, "display_name": name,
        })

    async def get_link(self, chat_id: str) -> dict[str, Any]:
        return await self._call(
            "GET", "/api/bot/link", params={"channel": self._channel, "chat_id": chat_id}
        )

    async def revoke(self, chat_id: str):
        return await self._call(
            "POST", "/api/bot/link/revoke", json={"channel": self._channel, "chat_id": chat_id}
        )

    async def mark_blocked(self, chat_id: str):
        return await self._call(
            "POST", "/api/bot/link/blocked", json={"channel": self._channel, "chat_id": chat_id}
        )

    # --- agents and sessions ------------------------------------------------

    async def agents(self, chat_id: str) -> dict[str, Any]:
        return await self._call(
            "GET", "/api/bot/agents", params={"channel": self._channel, "chat_id": chat_id}
        )

    async def set_session(
        self, chat_id: str, preset_id: Optional[str] = None, *, new_thread: bool = False
    ) -> dict[str, Any]:
        return await self._call("POST", "/api/bot/session", json={
            "channel": self._channel, "chat_id": chat_id,
            "preset_id": preset_id, "new_thread": new_thread,
        })

    # --- the turn -----------------------------------------------------------

    async def turn(
        self, chat_id: str, content: str, *,
        attachment: Optional[dict[str, Any]] = None, timeout: float,
    ) -> dict[str, Any]:
        """Run one agent turn. Long by nature — the platform assembles the
        reply, so the narration rule lives in exactly one place.

        attachment is {filename, mime_type, data_b64} for a file turn (the
        caption, possibly empty, is `content`). The key is left out entirely
        for a text turn, so a text turn is byte-for-byte what it always was
        and works against a backend that has never heard of files."""
        body: dict[str, Any] = {"channel": self._channel, "chat_id": chat_id,
                                "content": content}
        if attachment is not None:
            body["attachment"] = attachment
        return await self._call("POST", "/api/bot/turn", json=body, timeout=timeout)

    async def confirm(self, chat_id: str, pending_id: str, action: str) -> dict[str, Any]:
        """Approve or reject. An approve answers {status, tool_name, after}
        and, from a backend that knows them, `reply`, `vessel_outputs` and
        `files` saying what the change did. An older one sends none of them."""
        return await self._call("POST", "/api/bot/confirm", json={
            "channel": self._channel, "chat_id": chat_id,
            "pending_id": pending_id, "action": action,
        }, timeout=CONFIRM_TIMEOUT)

    # --- report files -------------------------------------------------------

    async def file(self, chat_id: str, export_id: int) -> tuple[bytes, str, str]:
        """(bytes, mime, filename) of one export a turn or an approve listed
        under `files`. POST so the chat id stays out of access logs.

        Errors map exactly as every other call: PlatformUnavailable when the
        backend can't be reached, PlatformError with the backend's sentence
        as `detail` otherwise (404 not on this desk, 410 removed). A body over
        FILE_MAX_BYTES is a PlatformError(413) without reading the rest."""
        body = {"channel": self._channel, "chat_id": chat_id, "export_id": int(export_id)}
        try:
            async with self._http.stream(
                "POST", f"{self._base}/api/bot/files", headers=self._headers, json=body,
            ) as r:
                if r.status_code >= 400:
                    await r.aread()
                    raise _error(r)
                declared = r.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > FILE_MAX_BYTES:
                    raise PlatformError(413, "file_too_large")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > FILE_MAX_BYTES:
                        raise PlatformError(413, "file_too_large")
                mime = r.headers.get("content-type", "").split(";")[0].strip().lower()
                name = filename_from(r.headers.get("content-disposition"), mime)
        except PlatformError:
            raise
        except Exception as exc:
            raise PlatformUnavailable(0, type(exc).__name__) from exc
        return bytes(buf), mime, name


def _error(r: httpx.Response) -> PlatformError:
    try:
        detail = r.json().get("detail")
    except Exception:
        detail = r.text[:120]
    wait = r.headers.get("retry-after", "").strip()
    return PlatformError(r.status_code, detail,
                         retry_after=int(wait) if wait.isdigit() else None)


def filename_from(disposition: Optional[str], mime: str) -> str:
    """The file name a Content-Disposition gives: the RFC 5987 `filename*`
    first (it carries the real, non-ASCII name), then the plain `filename`,
    then "Period Rundown.<ext>". Never a path: separators and control
    characters are dropped, since the name is shown to the user as is."""
    ext = _EXT.get(mime)
    fallback = f"Period Rundown.{ext}" if ext else "Period Rundown"
    value = disposition or ""
    name = None
    star = _FILENAME_STAR.search(value)
    if star:
        raw = star.group(1).strip().strip('"')
        charset, sep, rest = raw.partition("'")
        if sep:
            _lang, sep, encoded = rest.partition("'")
            if sep:
                try:
                    name = unquote(encoded, encoding=charset or "utf-8", errors="strict")
                except (LookupError, UnicodeDecodeError):
                    name = None
    if not name:
        plain = _FILENAME.search(value)
        if plain:
            quoted, bare = plain.group(1), plain.group(2)
            name = re.sub(r"\\(.)", r"\1", quoted) if quoted is not None else (bare or "").strip()
    name = "".join(c for c in (name or "") if c >= " " and c != "\x7f" and c not in "/\\")
    name = name.strip(" .")
    if len(name) > 200:  # keep the extension, which is what opens the file
        stem, dot, ext_ = name.rpartition(".")
        name = (stem[:200 - len(ext_) - 1] + dot + ext_) if dot and len(ext_) <= 8 else name[:200]
    return name or fallback
