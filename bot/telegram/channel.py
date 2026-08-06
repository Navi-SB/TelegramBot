"""Telegram's implementation of the core Channel protocol.

Everything Telegram-shaped that used to live in dispatch.py is here: update
parsing, HTML rendering, inline keyboards, and the placeholder-edit progress
pattern ("⏳ Thinking…" morphing into the answer).
"""
from __future__ import annotations

from typing import Any, Optional

from .. import strings as S
from ..core.dispatch import Inbound, Progress, diff_lines
from .api import TelegramError
from .html import esc, md_to_html, split_html
from .keyboards import agent_picker, confirm_unlink, confirm_write


def parse_update(u: dict[str, Any]) -> Optional[Inbound]:
    """Normalize a Telegram update. Everything the core needs, nothing else."""
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
    status = (member.get("new_chat_member") or {}).get("status")

    m = u.get("message") or {}
    media = any(k in m for k in ("photo", "voice", "document", "sticker", "video", "audio"))

    return Inbound(
        event_id=str(u["update_id"]),
        chat_id=str(chat.get("id")),
        sender_id=str(frm.get("id", "")),
        sender_name=" ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])) or None,
        text=text,
        command=command,
        args=args or "",
        callback_data=cb.get("data") if cb else None,
        callback_id=cb.get("id") if cb else None,
        is_unsupported_media=media and not text,
        is_private=chat.get("type", "private") == "private",
        blocked=status in ("kicked", "left"),
    )


class TelegramProgress(Progress):
    """First chunk replaces the '⏳ Thinking…' placeholder; the rest are sends."""

    def __init__(self, tg, chat_id: str, placeholder: Optional[int]):
        super().__init__(chat_id, edit_first=bool(placeholder))
        self._tg = tg
        self._placeholder = placeholder

    async def _send(self, chunk: str) -> None:
        await self._tg.send_safe(self.chat_id, chunk)

    async def _send_first(self, chunk: str) -> None:
        if self._placeholder:
            await self._tg.edit_message_text(self.chat_id, self._placeholder, chunk)
        else:
            await self._tg.send_safe(self.chat_id, chunk)


class TelegramChannel:
    S = S

    def __init__(self, tg):
        self._tg = tg

    async def ack(self, ctx: Inbound) -> None:
        # Kill the button spinner first — Telegram allows ~30s but the UI
        # looks broken after about two.
        if ctx.callback_id:
            try:
                await self._tg.answer_callback(ctx.callback_id)
            except TelegramError:
                pass

    async def send(self, chat_id: str, text: str) -> None:
        await self._tg.send_safe(chat_id, text)

    async def send_agent_picker(
        self, chat_id: str, agents: list[dict[str, Any]], page: int, title: str
    ) -> None:
        await self._tg.send_safe(chat_id, title, reply_markup=agent_picker(agents, page))

    async def send_unlink_confirm(self, chat_id: str) -> None:
        await self._tg.send_safe(chat_id, S.UNLINK_CONFIRM, reply_markup=confirm_unlink())

    async def send_confirm_write(self, chat_id: str, pending: dict[str, Any]) -> None:
        await self._tg.send_safe(chat_id, diff_card(pending),
                                 reply_markup=confirm_write(pending["pending_id"]))

    async def begin_progress(self, ctx: Inbound) -> TelegramProgress:
        await self._tg.send_chat_action(ctx.chat_id)
        placeholder = await self._tg.send_safe(ctx.chat_id, S.THINKING)
        return TelegramProgress(self._tg, ctx.chat_id, placeholder)

    def format_markdown(self, md: str) -> list[str]:
        return split_html(md_to_html(md))

    def format_verbatim(self, text: str) -> list[str]:
        return split_html(f"<pre>{esc(text)}</pre>")


def diff_card(pending: dict[str, Any]) -> str:
    diff = pending.get("diff") or {}
    lines = [f"⚠️ <b>Confirm write</b> — <code>{esc(pending.get('tool', ''))}</code>", ""]
    lines += [esc(line) for line in diff_lines(pending)]
    if diff.get("warning"):
        lines += ["", f"🚨 {esc(str(diff['warning']))}"]
    body = "\n".join(lines)
    # No room for a 4096 split under a keyboard, so truncate rather than lose
    # the buttons — but say so, since approving a diff you can't fully see is
    # exactly the failure mode worth avoiding on a phone.
    if len(body) > 3500:
        body = body[:3400] + "\n\n<i>… truncated. Review this one in the web app.</i>"
    return body
