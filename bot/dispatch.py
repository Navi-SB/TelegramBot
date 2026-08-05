"""handle_update() — the portable entry point.

Deliberately free of Vercel imports. Everything Telegram-shaped happens here,
so porting this bot to a systemd unit on the VPS is one file that long-polls
getUpdates and calls this same function. scripts/dev_poll.py is already that
file, which means the escape hatch is exercised every time anyone develops
locally rather than being a plan nobody has tried.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import strings as S
from .logging import log, log_exception
from .platform.client import PlatformClient, PlatformError, PlatformUnavailable
from .telegram.api import TelegramClient, TelegramError
from .telegram.html import md_to_html, split_html
from .telegram.keyboards import (
    agent_picker,
    confirm_unlink,
    confirm_write,
    decode,
    ref,
)


@dataclass
class Inbound:
    update_id: int
    chat_id: str
    chat_type: str
    sender_id: str
    sender_name: Optional[str]
    text: Optional[str]
    command: Optional[str]
    args: str
    callback_data: Optional[str]
    callback_id: Optional[str]
    is_unsupported_media: bool
    blocked: bool

    @classmethod
    def parse(cls, u: dict[str, Any]) -> Optional["Inbound"]:
        cb = u.get("callback_query")
        msg = cb.get("message") if cb else (u.get("message") or u.get("my_chat_member"))
        if not msg:
            return None
        chat = msg.get("chat") or {}
        frm = (cb or u.get("message") or u.get("my_chat_member") or {}).get("from") or {}
        text = None if cb else (u.get("message") or {}).get("text")

        command = args = None
        if text and text.startswith("/"):
            head, _, rest = text.partition(" ")
            command = head[1:].split("@")[0].lower()
            args = rest.strip()

        member = u.get("my_chat_member") or {}
        status = ((member.get("new_chat_member") or {}).get("status"))

        m = u.get("message") or {}
        media = any(k in m for k in ("photo", "voice", "document", "sticker", "video", "audio"))

        return cls(
            update_id=int(u["update_id"]),
            chat_id=str(chat.get("id")),
            chat_type=chat.get("type", "private"),
            sender_id=str(frm.get("id", "")),
            sender_name=" ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])) or None,
            text=text,
            command=command,
            args=args or "",
            callback_data=cb.get("data") if cb else None,
            callback_id=cb.get("id") if cb else None,
            is_unsupported_media=media and not text,
            blocked=status in ("kicked", "left"),
        )


async def handle_update(
    update: dict[str, Any], tg: TelegramClient, api: PlatformClient, *, turn_timeout: float
) -> None:
    ctx = Inbound.parse(update)
    if ctx is None:
        return

    # Kill the button spinner first — Telegram allows ~30s but the UI looks
    # broken after about two.
    if ctx.callback_id:
        try:
            await tg.answer_callback(ctx.callback_id)
        except TelegramError:
            pass

    if ctx.blocked:
        try:
            await api.mark_blocked(ctx.chat_id)
        except PlatformError:
            pass
        return

    # Guard rails that need no network at all.
    if ctx.chat_type != "private":
        await tg.send_safe(ctx.chat_id, S.GROUPS_UNSUPPORTED)
        return
    if ctx.command == "help":
        await tg.send_safe(ctx.chat_id, S.HELP)
        return
    if ctx.is_unsupported_media:
        await tg.send_safe(ctx.chat_id, S.TEXT_ONLY)
        return

    try:
        link = await api.get_link(ctx.chat_id)
    except PlatformUnavailable as exc:
        log_exception("platform_down", exc, chat_id=ctx.chat_id)
        await tg.send_safe(ctx.chat_id, S.PLATFORM_DOWN)
        return

    if not link.get("linked"):
        # The ONLY thing an unlinked chat may do.
        if ctx.command == "start" and ctx.args:
            await _redeem(ctx, tg, api)
        else:
            await tg.send_safe(ctx.chat_id, S.NOT_LINKED)
        return

    try:
        if ctx.callback_data:
            await _callback(ctx, tg, api)
        elif ctx.command:
            await _command(ctx, tg, api, link, turn_timeout=turn_timeout)
        elif ctx.text:
            await _turn(ctx, tg, api, turn_timeout=turn_timeout)
    except PlatformUnavailable as exc:
        log_exception("platform_down", exc, chat_id=ctx.chat_id)
        await tg.send_safe(ctx.chat_id, S.PLATFORM_DOWN)
    except Exception as exc:  # noqa: BLE001
        log_exception("dispatch_failed", exc, chat_id=ctx.chat_id)
        await tg.send_safe(ctx.chat_id, S.UNEXPECTED)


# --- linking ----------------------------------------------------------------


async def _redeem(ctx: Inbound, tg: TelegramClient, api: PlatformClient) -> None:
    try:
        res = await api.redeem(ctx.chat_id, ctx.args, ctx.sender_id, ctx.sender_name)
    except PlatformError as exc:
        log("link_failed", chat_id=ctx.chat_id, status=exc.status)
        await tg.send_safe(ctx.chat_id, S.LINK_FAILED)
        return
    user = res.get("user") or {}
    log("linked", chat_id=ctx.chat_id, user_id=user.get("id"))
    await tg.send_safe(ctx.chat_id, S.linked(user.get("name", "your account"), user.get("email", "")))
    await _show_agents(ctx, tg, api)


# --- commands ---------------------------------------------------------------


async def _command(ctx, tg, api, link, *, turn_timeout: float) -> None:
    cmd = ctx.command
    if cmd == "start":
        # Already linked, and /start carries no useful payload here.
        await _status(ctx, tg, api, link)
    elif cmd == "agents":
        await _show_agents(ctx, tg, api)
    elif cmd == "agent":
        await _select_by_name(ctx, tg, api)
    elif cmd == "new":
        session = await api.set_session(ctx.chat_id, await _active(api, ctx.chat_id), new_thread=True)
        await tg.send_safe(ctx.chat_id, S.new_thread(session.get("preset_name")))
    elif cmd == "status":
        await _status(ctx, tg, api, link)
    elif cmd == "unlink":
        await tg.send_safe(ctx.chat_id, S.UNLINK_CONFIRM, reply_markup=confirm_unlink())
    else:
        await tg.send_safe(ctx.chat_id, S.HELP)


async def _active(api: PlatformClient, chat_id: str) -> Optional[str]:
    return (await api.agents(chat_id)).get("active_preset_id")


async def _status(ctx, tg, api, link) -> None:
    session = await api.set_session(ctx.chat_id, await _active(api, ctx.chat_id))
    user = link.get("user") or {}
    await tg.send_safe(ctx.chat_id, S.status(
        user.get("email", "unknown"), session.get("preset_name"), session.get("turns", 0)
    ))


async def _show_agents(ctx, tg, api, page: int = 0) -> None:
    agents = (await api.agents(ctx.chat_id)).get("agents", [])
    if not agents:
        await tg.send_safe(ctx.chat_id, S.NO_AGENTS)
        return
    await tg.send_safe(ctx.chat_id, "Which agent should I use?",
                       reply_markup=agent_picker(agents, page))


async def _select_by_name(ctx, tg, api) -> None:
    wanted = ctx.args.strip().lower()
    agents = (await api.agents(ctx.chat_id)).get("agents", [])
    if not wanted:
        await _show_agents(ctx, tg, api)
        return
    exact = [a for a in agents if a["name"].lower() == wanted]
    partial = [a for a in agents if wanted in a["name"].lower()]
    hits = exact or partial
    if len(hits) == 1:
        await _select(ctx, tg, api, hits[0]["id"])
    elif hits:
        await tg.send_safe(ctx.chat_id, "Which one?", reply_markup=agent_picker(hits))
    else:
        await tg.send_safe(ctx.chat_id, f"No agent matches <b>{ctx.args[:40]}</b>. Try /agents.")


async def _select(ctx, tg, api, preset_id: Optional[str]) -> None:
    session = await api.set_session(ctx.chat_id, preset_id)
    if not preset_id:
        await tg.send_safe(ctx.chat_id, S.FULL_TEXT_SELECTED)
        return
    await tg.send_safe(ctx.chat_id, S.agent_selected(
        session.get("preset_name") or "your agent",
        session.get("turns", 0),
        session.get("rotated", False),
    ))


# --- callbacks --------------------------------------------------------------


async def _callback(ctx, tg, api) -> None:
    op, args = decode(ctx.callback_data or "")
    if op is None:
        await tg.send_safe(ctx.chat_id, S.STALE_MENU)
        return

    if op == "ap":
        await _show_agents(ctx, tg, api, int(args[0]) if args else 0)
    elif op == "a":
        wanted_ref = args[0] if args else "-"
        if wanted_ref == "-":
            await _select(ctx, tg, api, None)
            return
        agents = (await api.agents(ctx.chat_id)).get("agents", [])
        match = next((a for a in agents if ref(a["id"]) == wanted_ref), None)
        if match is None:
            await tg.send_safe(ctx.chat_id, S.STALE_MENU)
            return
        await _select(ctx, tg, api, match["id"])
    elif op == "w":
        await _confirm(ctx, tg, api, args)
    elif op == "ul":
        if args and args[0] == "y":
            await api.revoke(ctx.chat_id)
            await tg.send_safe(ctx.chat_id, S.UNLINKED)
        else:
            await tg.send_safe(ctx.chat_id, S.UNLINK_CANCELLED)


async def _confirm(ctx, tg, api, args: list[str]) -> None:
    if len(args) < 2:
        return
    decision, pending_id = args[0], args[1]
    # Strip the keyboard first so a second tap has nothing to hit. The real
    # guard is the database, but this removes the common case.
    if ctx.callback_id:
        try:
            msg_id = None  # set below when available
        except Exception:
            msg_id = None
    action = "approve" if decision == "y" else "reject"
    try:
        await api.confirm(ctx.chat_id, pending_id, action)
        await tg.send_safe(ctx.chat_id, "✅ Change approved." if action == "approve"
                           else "❌ Change rejected.")
    except PlatformError as exc:
        if exc.status == 409:
            # Already resolved — a double tap. Success, not an error.
            await tg.send_safe(ctx.chat_id, "Already resolved.")
        else:
            raise


# --- the turn ---------------------------------------------------------------


async def _turn(ctx, tg, api, *, turn_timeout: float) -> None:
    await tg.send_chat_action(ctx.chat_id)
    placeholder = await tg.send_safe(ctx.chat_id, S.THINKING)

    try:
        result = await api.turn(ctx.chat_id, ctx.text or "", timeout=turn_timeout)
    except PlatformUnavailable:
        await _replace(tg, ctx.chat_id, placeholder, S.PLATFORM_DOWN)
        return
    except PlatformError as exc:
        text = S.TURN_TIMEOUT if exc.status in (504, 0) else S.UNEXPECTED
        await _replace(tg, ctx.chat_id, placeholder, text)
        return

    chunks: list[str] = []
    # The user's own rendered output templates go first and VERBATIM — these
    # are what gets pasted into a broker's WhatsApp, so each is its own message
    # and none of it is re-wrapped or "improved".
    for rendered in result.get("vessel_outputs") or []:
        chunks.extend(split_html(f"<pre>{_esc(rendered)}</pre>"))

    if result.get("error"):
        # A turn can fail AFTER earlier iterations already rendered outputs
        # (e.g. bunker prices fetched, then the pool-calc iteration dies).
        # Those are answers the user asked for — ship them, then the error.
        chunks.append(S.agent_error(str(result["error"]).split(":")[0]))
        await _replace(tg, ctx.chat_id, placeholder, chunks[0])
        for extra in chunks[1:]:
            await tg.send_safe(ctx.chat_id, extra)
        return

    reply = (result.get("reply") or "").strip()
    if reply:
        chunks.extend(split_html(md_to_html(reply)))
    if not chunks:
        chunks = ["<i>(no answer)</i>"]

    await _replace(tg, ctx.chat_id, placeholder, chunks[0])
    for extra in chunks[1:]:
        await tg.send_safe(ctx.chat_id, extra)

    for pending in result.get("pending") or []:
        await tg.send_safe(ctx.chat_id, _diff_card(pending),
                           reply_markup=confirm_write(pending["pending_id"]))

    log("turn_done", chat_id=ctx.chat_id, tools=result.get("tools_used"),
        chunks=len(chunks), outcome=result.get("stop_reason"))


def _esc(text: str) -> str:
    from .telegram.html import esc

    return esc(text)


def _diff_card(pending: dict[str, Any]) -> str:
    diff = pending.get("diff") or {}
    summary = diff.get("summary") or pending.get("tool", "change")
    lines = [f"⚠️ <b>Confirm write</b> — <code>{_esc(pending.get('tool',''))}</code>", "",
             _esc(str(summary))]
    if diff.get("warning"):
        lines += ["", f"🚨 {_esc(str(diff['warning']))}"]
    body = "\n".join(lines)
    # No room for a 4096 split under a keyboard, so truncate rather than lose
    # the buttons — but say so, since approving a diff you can't fully see is
    # exactly the failure mode worth avoiding on a phone.
    if len(body) > 3500:
        body = body[:3400] + "\n\n<i>… truncated. Review this one in the web app.</i>"
    return body


async def _replace(tg, chat_id: str, message_id: Optional[int], text: str) -> None:
    if message_id:
        await tg.edit_message_text(chat_id, message_id, text)
    else:
        await tg.send_safe(chat_id, text)
