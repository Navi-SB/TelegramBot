"""Telegram's implementation of the core Channel protocol.

Everything Telegram-shaped that used to live in dispatch.py is here: update
parsing, HTML rendering, inline keyboards, and the placeholder-edit progress
pattern ("⏳ Thinking…" morphing into the answer).
"""
from __future__ import annotations

from typing import Any, Optional

from .. import strings as S
from ..core.attachments import Attachment, AttachmentTooLarge, AttachmentUnavailable
from ..core.dispatch import Inbound, Progress, diff_lines
from .api import TelegramError
from .html import esc, md_to_html, split_html
from .keyboards import agent_picker, confirm_unlink, confirm_write


# Checked in this order, and before "document": Telegram sets `document` on a
# GIF too (animation, kept for old clients), and a GIF is not a file to read.
_MEDIA_KEYS = ("animation", "photo", "voice", "video_note", "sticker", "video", "audio")


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
    media_kind = next((k for k in _MEDIA_KEYS if k in m), None)
    media = m.get(media_kind) if media_kind else None
    media_mime = media.get("mime_type") if isinstance(media, dict) else None  # photo is a list

    attachment = None
    doc = m.get("document")
    if not cb and media_kind is None and isinstance(doc, dict) and doc.get("file_id"):
        # A document's words are in `caption`, not `text`. The caption is
        # what the user wants done with the file — never a command: "/new"
        # written under a PDF is an instruction about the PDF.
        attachment = Attachment(
            ref=str(doc["file_id"]),
            file_name=doc.get("file_name"),
            mime_hint=doc.get("mime_type"),
            size_hint=doc.get("file_size") if isinstance(doc.get("file_size"), int) else None,
        )
        text = m.get("caption")
        command = args = None

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
        is_unsupported_media=media_kind is not None and not text,
        is_private=chat.get("type", "private") == "private",
        blocked=status in ("kicked", "left"),
        attachment=attachment,
        media_kind=media_kind,
        media_mime=media_mime,
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
        self, chat_id: str, agents: list[dict[str, Any]], page: int, title: str,
        *, extras: bool = True,
    ) -> None:
        await self._tg.send_safe(chat_id, title,
                                 reply_markup=agent_picker(agents, page, extras=extras))

    async def send_unlink_confirm(self, chat_id: str) -> None:
        await self._tg.send_safe(chat_id, S.UNLINK_CONFIRM, reply_markup=confirm_unlink())

    async def send_confirm_write(self, chat_id: str, pending: dict[str, Any]) -> None:
        await self._tg.send_safe(chat_id, diff_card(pending),
                                 reply_markup=confirm_write(pending["pending_id"]))

    async def begin_progress(self, ctx: Inbound) -> TelegramProgress:
        await self._tg.send_chat_action(ctx.chat_id)
        # A file turn spends its first seconds downloading; say what's
        # happening. The placeholder becomes the answer either way.
        waiting = S.READING_FILE if ctx.attachment is not None else S.THINKING
        placeholder = await self._tg.send_safe(ctx.chat_id, waiting)
        return TelegramProgress(self._tg, ctx.chat_id, placeholder)

    async def fetch_attachment(self, att: Attachment, max_bytes: int) -> bytes:
        try:
            info = await self._tg.get_file(att.ref)
        except TelegramError as exc:
            # Over the cloud Bot API's 20 MB, Telegram won't even hand out a
            # path — which is still "too big" to the user, not "try again".
            if "too big" in exc.description.lower():
                raise AttachmentTooLarge("platform_too_large") from None
            raise AttachmentUnavailable(f"getfile_{exc.code}") from None
        except Exception as exc:  # noqa: BLE001 — transport; its message may hold the URL
            raise AttachmentUnavailable(type(exc).__name__) from None

        size = info.get("file_size")
        if isinstance(size, int) and size > max_bytes:
            raise AttachmentTooLarge()
        path = info.get("file_path")
        if not path:
            raise AttachmentUnavailable("no_file_path")
        return await self._tg.download_file(path, max_bytes)

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
