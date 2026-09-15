"""The channel-neutral bot core.

Every flow that is the PRODUCT — linking, agent selection, sessions, the turn,
write confirmation, unlink — lives here exactly once. What differs per channel
(parsing an update, rendering markdown, building a keyboard vs a list message,
editing a placeholder vs plain sends) hides behind the Channel protocol, so a
bugfix to the unlinked gate or the 409 handling lands on Telegram and WhatsApp
in the same commit.

The Telegram behaviour this was extracted from is pinned by tests/ — they run
against the public bot.dispatch.handle_update seam and passed unmodified
across the extraction.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from ..logging import log, log_exception
from ..platform.client import PlatformClient, PlatformError, PlatformUnavailable
from .attachments import (
    MAX_BYTES, Attachment, AttachmentError, AttachmentTooLarge, to_payload,
)
from .codec import decode, ref

# A download gets at most this share of the turn deadline, and never more than
# this many seconds: whatever it uses comes off the time the agent gets, and a
# file that takes a minute to arrive is not going to arrive.
_FETCH_SHARE = 0.5
_FETCH_MAX_SECONDS = 60.0

_MIME_SHAPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")


@dataclass
class Inbound:
    """One normalized inbound event, whatever channel it came from."""

    event_id: str
    chat_id: str
    sender_id: str
    sender_name: Optional[str]
    text: Optional[str]
    command: Optional[str]
    args: str
    callback_data: Optional[str]  # tapped button/list-row payload (codec string)
    callback_id: Optional[str]    # channel handle for acking the tap
    is_unsupported_media: bool
    is_private: bool = True
    blocked: bool = False         # Telegram-only; WhatsApp learns it send-side
    log_ref: str = ""             # what logs show for this chat; never a phone number
    # A PDF or Word file (or anything sent AS a file — the backend decides).
    # Its caption, if any, is `text`, and is never read as a command.
    attachment: Optional[Attachment] = None
    # What an unsupported message was ("photo", "sticker") and its declared
    # type, for the media_refused log line only.
    media_kind: Optional[str] = None
    media_mime: Optional[str] = None

    def __post_init__(self):
        if not self.log_ref:
            self.log_ref = self.chat_id


class Progress:
    """Delivers an ordered sequence of chunks with per-message isolation.

    One failed send must never cost the rest of the answer, and a failed
    placeholder edit must never discard the turn. Channels override _send
    (and optionally _send_first, e.g. to edit a placeholder).
    """

    def __init__(self, chat_id: str, log_ref: str | None = None, *, edit_first: bool = False):
        self.chat_id = chat_id
        self.log_ref = log_ref or chat_id
        self.delivered = 0
        self._first = True
        # Only a channel whose first send EDITS something (Telegram's
        # placeholder) gets the edit-failed→plain-send fallback. On a
        # send-only channel that fallback would double-send the first chunk
        # whenever the first attempt failed after actually reaching the user.
        self._edit_first = edit_first

    async def _send(self, chunk: str):  # pragma: no cover — abstract
        raise NotImplementedError

    async def _send_first(self, chunk: str):
        return await self._send(chunk)

    async def emit(self, chunk: str) -> bool:
        try:
            if self._first and self._edit_first:
                try:
                    res = await self._send_first(chunk)
                except Exception as exc:  # noqa: BLE001
                    # The edit failing must not eat the answer — send it plain.
                    log_exception("placeholder_edit_failed", exc, chat_id=self.log_ref)
                    res = await self._send(chunk)
            else:
                res = await self._send(chunk)
            if res is False:
                # The channel swallowed the send (and already logged why) —
                # it must not count as delivered or turn_done lies.
                return False
            self.delivered += 1
            return True
        except Exception as exc:  # noqa: BLE001 — isolate per message
            log_exception("chunk_send_failed", exc, chat_id=self.log_ref)
            return False
        finally:
            self._first = False


class Channel(Protocol):
    """What a messaging channel must provide for the core to drive it."""

    S: Any  # the channel's strings module

    async def ack(self, ctx: Inbound) -> None: ...
    async def send(self, chat_id: str, text: str) -> None: ...
    async def send_agent_picker(
        self, chat_id: str, agents: list[dict[str, Any]], page: int, title: str,
        *, extras: bool = True,
    ) -> None: ...
    async def send_unlink_confirm(self, chat_id: str) -> None: ...
    async def send_confirm_write(self, chat_id: str, pending: dict[str, Any]) -> None: ...
    async def begin_progress(self, ctx: "Inbound") -> Progress: ...
    async def fetch_attachment(self, att: Attachment, max_bytes: int) -> bytes:
        """The file's bytes, or AttachmentTooLarge / AttachmentUnavailable.
        Nothing else may escape: the core turns those two into messages."""
        ...
    def format_markdown(self, md: str) -> list[str]: ...
    def format_verbatim(self, text: str) -> list[str]: ...


async def handle_inbound(
    ctx: Inbound, ch: Channel, api: PlatformClient, *, turn_timeout: float
) -> None:
    S = ch.S
    await ch.ack(ctx)

    if ctx.blocked:
        try:
            await api.mark_blocked(ctx.chat_id)
        except PlatformError:
            pass
        return

    # Guard rails that need no network at all.
    if not ctx.is_private:
        await ch.send(ctx.chat_id, S.GROUPS_UNSUPPORTED)
        return
    if ctx.command == "help":
        await ch.send(ctx.chat_id, S.HELP)
        return
    if ctx.is_unsupported_media:
        # The kind and declared type only — never a file name or caption.
        # This is how demand for photos and voice notes gets measured.
        log("media_refused", chat_id=ctx.log_ref, kind=ctx.media_kind,
            mime=_loggable_mime(ctx.media_mime))
        await ch.send(ctx.chat_id, S.TEXT_ONLY)
        return

    try:
        link = await api.get_link(ctx.chat_id)
    except PlatformUnavailable as exc:
        log_exception("platform_down", exc, chat_id=ctx.log_ref)
        await ch.send(ctx.chat_id, S.PLATFORM_DOWN)
        return

    try:
        if not link.get("linked"):
            # The ONLY thing an unlinked chat may do. Inside the guard: the
            # post-pairing agent list hits the platform too, and a failure
            # there must degrade to a message, not to silence after
            # "Connected".
            if ctx.command == "start" and ctx.args:
                await _redeem(ctx, ch, api)
            else:
                await ch.send(ctx.chat_id, S.NOT_LINKED)
        elif ctx.callback_data:
            await _callback(ctx, ch, api)
        elif ctx.attachment is not None:
            # Only here, past the link check: an unlinked chat must never be
            # able to make the bot download anything.
            await _turn(ctx, ch, api, turn_timeout=turn_timeout)
        elif ctx.command:
            await _command(ctx, ch, api, link, turn_timeout=turn_timeout)
        elif ctx.text:
            await _turn(ctx, ch, api, turn_timeout=turn_timeout)
    except PlatformUnavailable as exc:
        log_exception("platform_down", exc, chat_id=ctx.log_ref)
        await ch.send(ctx.chat_id, S.PLATFORM_DOWN)
    except Exception as exc:  # noqa: BLE001
        log_exception("dispatch_failed", exc, chat_id=ctx.log_ref)
        await ch.send(ctx.chat_id, S.UNEXPECTED)


# --- linking ----------------------------------------------------------------


async def _redeem(ctx: Inbound, ch: Channel, api: PlatformClient) -> None:
    try:
        res = await api.redeem(ctx.chat_id, ctx.args, ctx.sender_id, ctx.sender_name)
    except PlatformUnavailable:
        # The backend never saw the code, so it wasn't burned. LINK_FAILED
        # would send the user off to regenerate a still-valid link; let the
        # outer guard say the platform is down and invite a retry instead.
        raise
    except PlatformError as exc:
        log("link_failed", chat_id=ctx.log_ref, status=exc.status)
        await ch.send(ctx.chat_id, ch.S.LINK_FAILED)
        return
    user = res.get("user") or {}
    log("linked", chat_id=ctx.log_ref, user_id=user.get("id"))
    await ch.send(ctx.chat_id, ch.S.linked(user.get("name", "your account"), user.get("email", "")))
    await _show_agents(ctx, ch, api)


# --- commands ---------------------------------------------------------------


async def _command(ctx, ch, api, link, *, turn_timeout: float) -> None:
    cmd = ctx.command
    if cmd == "start":
        # Already linked, and /start carries no useful payload here.
        await _status(ctx, ch, api, link)
    elif cmd == "agents":
        await _show_agents(ctx, ch, api)
    elif cmd == "agent":
        await _select_by_name(ctx, ch, api)
    elif cmd == "new":
        session = await api.set_session(ctx.chat_id, await _active(api, ctx.chat_id), new_thread=True)
        await ch.send(ctx.chat_id, ch.S.new_thread(session.get("preset_name")))
    elif cmd == "status":
        await _status(ctx, ch, api, link)
    elif cmd == "unlink":
        await ch.send_unlink_confirm(ctx.chat_id)
    elif cmd in ("short", "shortdesc", "shortdescr"):
        await _show_tools(ctx, ch, api)
    else:
        await ch.send(ctx.chat_id, ch.S.HELP)


async def _active(api: PlatformClient, chat_id: str) -> Optional[str]:
    return (await api.agents(chat_id)).get("active_preset_id")


async def _status(ctx, ch, api, link) -> None:
    session = await api.set_session(ctx.chat_id, await _active(api, ctx.chat_id))
    user = link.get("user") or {}
    await ch.send(ctx.chat_id, ch.S.status(
        user.get("email", "unknown"), session.get("preset_name"), session.get("turns", 0)
    ))


async def _show_agents(ctx, ch, api, page: int = 0) -> None:
    agents = (await api.agents(ctx.chat_id)).get("agents", [])
    if not agents:
        await ch.send(ctx.chat_id, ch.S.NO_AGENTS)
        return
    await ch.send_agent_picker(ctx.chat_id, agents, page, ch.S.PICK_AGENT)


async def _show_tools(ctx, ch, api) -> None:
    """/short — the Short Description Generator's format picker.

    The formats come from the platform's `tools` key, deliberately separate
    from the user's agents: /agents stays agents-only, and a new format never
    needs a bot deploy. An older platform without the key gets a plain
    "not available yet" rather than an empty menu."""
    res = await api.agents(ctx.chat_id)
    tools = res.get("tools") or []
    if not tools:
        await ch.send(ctx.chat_id, ch.S.TOOLS_UNAVAILABLE)
        return
    wanted = ctx.args.strip().lower()
    if wanted:
        hits = [t for t in tools
                if wanted in t["name"].lower() or t["id"].rsplit(":", 1)[-1] == wanted]
        if len(hits) == 1:
            await _select(ctx, ch, api, hits[0]["id"])
            return
        tools = hits or tools
    await ch.send_agent_picker(ctx.chat_id, tools, 0, ch.S.PICK_FORMAT, extras=False)


async def _select_by_name(ctx, ch, api) -> None:
    wanted = ctx.args.strip().lower()
    agents = (await api.agents(ctx.chat_id)).get("agents", [])
    if not wanted:
        await _show_agents(ctx, ch, api)
        return
    exact = [a for a in agents if a["name"].lower() == wanted]
    partial = [a for a in agents if wanted in a["name"].lower()]
    hits = exact or partial
    if len(hits) == 1:
        await _select(ctx, ch, api, hits[0]["id"])
    elif hits:
        await ch.send_agent_picker(ctx.chat_id, hits, 0, ch.S.PICK_WHICH)
    else:
        await ch.send(ctx.chat_id, ch.S.no_agent_match(ctx.args[:40]))


async def _select(ctx, ch, api, preset_id: Optional[str]) -> None:
    session = await api.set_session(ctx.chat_id, preset_id)
    if not preset_id:
        await ch.send(ctx.chat_id, ch.S.FULL_TEXT_SELECTED)
        return
    name = session.get("preset_name") or "your agent"
    if preset_id.startswith("tool:"):
        # A generator mode, not a conversation — say what to paste next
        # instead of announcing thread state.
        await ch.send(ctx.chat_id, ch.S.tool_selected(name))
        return
    await ch.send(ctx.chat_id, ch.S.agent_selected(
        name,
        session.get("turns", 0),
        session.get("rotated", False),
    ))


# --- callbacks --------------------------------------------------------------


async def _callback(ctx, ch, api) -> None:
    op, args = decode(ctx.callback_data or "")
    if op is None:
        await ch.send(ctx.chat_id, ch.S.STALE_MENU)
        return

    if op == "ap":
        await _show_agents(ctx, ch, api, int(args[0]) if args else 0)
    elif op == "a":
        wanted_ref = args[0] if args else "-"
        if wanted_ref == "-":
            await _select(ctx, ch, api, None)
            return
        res = await api.agents(ctx.chat_id)
        pool = (res.get("agents") or []) + (res.get("tools") or [])
        match = next((a for a in pool if ref(a["id"]) == wanted_ref), None)
        if match is None:
            await ch.send(ctx.chat_id, ch.S.STALE_MENU)
            return
        await _select(ctx, ch, api, match["id"])
    elif op == "w":
        await _confirm(ctx, ch, api, args)
    elif op == "ul":
        if args and args[0] == "y":
            await api.revoke(ctx.chat_id)
            await ch.send(ctx.chat_id, ch.S.UNLINKED)
        else:
            await ch.send(ctx.chat_id, ch.S.UNLINK_CANCELLED)


def _is_already_resolved(detail: Any) -> bool:
    """A 409 from /api/bot/confirm is NOT always a double tap. apply_pending
    raises for 'is approved, not pending' (double tap — success) but also for
    real apply failures ('Vessel X not found.') where the row was handed back
    and is retryable. Reading every 409 as success masked those entirely."""
    d = str(detail or "").lower()
    return "already" in d or "not pending" in d


async def _confirm(ctx, ch, api, args: list[str]) -> None:
    if len(args) < 2:
        return
    decision, pending_id = args[0], args[1]
    action = "approve" if decision == "y" else "reject"
    try:
        await api.confirm(ctx.chat_id, pending_id, action)
        await ch.send(ctx.chat_id,
                      ch.S.APPROVED if action == "approve" else ch.S.REJECTED)
    except PlatformError as exc:
        if exc.status == 409 and _is_already_resolved(exc.detail):
            # A double tap. Success, not an error.
            await ch.send(ctx.chat_id, ch.S.ALREADY_RESOLVED)
        elif exc.status == 409:
            log("confirm_apply_failed", chat_id=ctx.log_ref, status=exc.status)
            await ch.send(ctx.chat_id, ch.S.confirm_failed(str(exc.detail or "")))
        else:
            raise


# --- the turn ---------------------------------------------------------------


async def _turn(ctx, ch, api, *, turn_timeout: float) -> None:
    started = time.monotonic()
    att: Optional[Attachment] = ctx.attachment
    if att is not None and att.size_hint and att.size_hint > MAX_BYTES:
        # Declared too big: say so without downloading a byte of it.
        log("attachment_failed", chat_id=ctx.log_ref, outcome="declared_too_large")
        await ch.send(ctx.chat_id, ch.S.FILE_TOO_LARGE)
        return

    prog = await ch.begin_progress(ctx)

    payload = None
    if att is not None:
        payload = await _fetch(ctx, ch, prog, att, turn_timeout)
        if payload is None:
            return

    # The download already spent part of the deadline; the turn gets the rest.
    remaining = max(1.0, turn_timeout - (time.monotonic() - started))
    try:
        result = await api.turn(ctx.chat_id, ctx.text or "",
                                attachment=payload, timeout=remaining)
    except PlatformUnavailable:
        await _deliver(prog, [ch.S.PLATFORM_DOWN])
        return
    except PlatformError as exc:
        if payload is not None and exc.status == 422:
            # A backend from before file support. It ignores the unknown
            # `attachment` key, so what it 422s on is the empty content of a
            # caption-less file. (Given a caption it would quietly answer the
            # caption alone — which is why the backend deploys first.) Say
            # "not yet", not "something went wrong": retrying won't help.
            text = ch.S.FILES_NOT_SUPPORTED_YET
        elif payload is not None and exc.status == 413:
            # Never the file's size as far as the user can act on it: nothing
            # over MAX_BYTES is ever sent, and the backend allows the same. It
            # is a proxy in front of the backend with a smaller body limit
            # (nginx's default is 1 MB), which only a deploy fixes.
            log("attachment_failed", chat_id=ctx.log_ref, outcome="upload_413")
            text = ch.S.FILE_NOT_DELIVERED
        elif exc.status in (504, 0):
            text = ch.S.TURN_TIMEOUT
        else:
            text = ch.S.UNEXPECTED
        await _deliver(prog, [text])
        return

    chunks: list[str] = []
    # The user's own rendered output templates go first and VERBATIM — these
    # are what gets pasted into a broker's WhatsApp, so each is its own message
    # and none of it is re-wrapped or "improved".
    for rendered in result.get("vessel_outputs") or []:
        chunks.extend(ch.format_verbatim(rendered))

    if result.get("error"):
        # A turn can fail AFTER earlier iterations already rendered outputs
        # (e.g. bunker prices fetched, then the pool-calc iteration dies).
        # Those are answers the user asked for — ship them, then the error.
        chunks.append(ch.S.agent_error(str(result["error"]).split(":")[0]))
        await _deliver(prog, chunks)
        return

    reply = (result.get("reply") or "").strip()
    if reply:
        chunks.extend(ch.format_markdown(reply))
    if not chunks:
        chunks = [ch.S.NO_ANSWER]

    await _deliver(prog, chunks)

    for pending in result.get("pending") or []:
        try:
            await ch.send_confirm_write(ctx.chat_id, pending)
        except Exception as exc:  # noqa: BLE001 — one lost card ≠ a lost turn
            log_exception("confirm_card_send_failed", exc, chat_id=ctx.log_ref)

    log("turn_done", chat_id=ctx.log_ref, tools=result.get("tools_used"),
        chunks=len(chunks), outcome=result.get("stop_reason"),
        kind="file" if payload is not None else "text")


async def _fetch(ctx, ch, prog: Progress, att: Attachment,
                 turn_timeout: float) -> Optional[dict[str, Any]]:
    """Download the file and shape it for /api/bot/turn — or tell the user
    why not and return None. The bytes live only as long as this turn."""
    budget = min(_FETCH_MAX_SECONDS, turn_timeout * _FETCH_SHARE)
    try:
        data = await asyncio.wait_for(ch.fetch_attachment(att, MAX_BYTES), budget)
    except AttachmentTooLarge as exc:
        log("attachment_failed", chat_id=ctx.log_ref, outcome=exc.reason)
        await _deliver(prog, [ch.S.FILE_TOO_LARGE])
        return None
    except AttachmentError as exc:
        log("attachment_failed", chat_id=ctx.log_ref, outcome=exc.reason)
        await _deliver(prog, [ch.S.FILE_UNAVAILABLE])
        return None
    except asyncio.TimeoutError:
        log("attachment_failed", chat_id=ctx.log_ref, outcome="timeout")
        await _deliver(prog, [ch.S.FILE_UNAVAILABLE])
        return None
    return to_payload(att, data)


def _loggable_mime(mime: Optional[str]) -> Optional[str]:
    """A declared type is sender-controlled text. Only something shaped like
    a MIME type reaches the logs; anything else is just "other"."""
    if not mime:
        return None
    m = mime.strip().lower()[:100]
    return m if _MIME_SHAPE.match(m) else "other"


async def _deliver(prog: Progress, chunks: list[str]) -> int:
    for chunk in chunks:
        await prog.emit(chunk)
    if prog.delivered < len(chunks):
        log("delivery_incomplete", chat_id=prog.log_ref,
            chunks=f"{prog.delivered}/{len(chunks)}")
    return prog.delivered


# --- confirm-card content (channel-neutral; channels do the styling) --------


def payload_lines(payload: dict[str, Any]) -> list[str]:
    """Flatten one before/after payload into readable lines.

    Fixture tools send {"count": N, "fixtures": [...]}; vessel tools send the
    record itself. Nulls and empties are noise on a phone screen — dropped.
    """
    fixtures = payload.get("fixtures")
    if isinstance(fixtures, list):
        lines = []
        for f in fixtures[:8]:
            if isinstance(f, dict):
                kv = ", ".join(f"{k}: {v}" for k, v in f.items() if v not in (None, ""))
                lines.append(f"• {kv[:200]}")
            else:
                lines.append(f"• {str(f)[:200]}")
        if len(fixtures) > 8:
            lines.append(f"… and {len(fixtures) - 8} more")
        return lines
    return [f"{k}: {v}" for k, v in payload.items() if v not in (None, "")][:15]


def diff_lines(pending: dict[str, Any]) -> list[str]:
    """What is actually about to change, from the shapes the server sends:
    {before, after, changed} — there is no 'summary' key."""
    diff = pending.get("diff") or {}
    if diff.get("summary"):  # honoured if a future server ever sends one
        return [str(diff["summary"])]

    before, after, changed = diff.get("before"), diff.get("after"), diff.get("changed")
    if isinstance(changed, dict) and changed:
        return [
            f"{k}: {v.get('from')} → {v.get('to')}" if isinstance(v, dict) else f"{k}: {v}"
            for k, v in changed.items()
        ]
    if before is None and isinstance(after, dict):
        count = after.get("count")
        head = f"Adding {count} item(s):" if count else "Adding:"
        return [head] + payload_lines(after)
    if after is None and isinstance(before, dict):
        count = before.get("count")
        head = f"Removing {count} item(s):" if count else "Removing:"
        return [head] + payload_lines(before)
    if isinstance(after, dict):
        return payload_lines(after)
    return [str(pending.get("tool", "change"))]
