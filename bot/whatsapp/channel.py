"""WhatsApp's implementation of the core Channel protocol.

The structural differences from Telegram, all contained here:
  - No message editing → no placeholder. Progress is mark-read + typing on
    the inbound message, then plain buffered sends.
  - No inbound "blocked" event → WhatsAppUndeliverable (131026) on a send is
    the signal; it triggers mark_blocked once and further sends are skipped
    quietly (the user is unreachable — cascading errors help nobody).
  - Interactive body is capped at 1024 → the confirm card truncates at ~950,
    much harder than Telegram's 3500, with the same web-app pointer.
"""
from __future__ import annotations

from typing import Any

from ..core.dispatch import Inbound, Progress, diff_lines
from ..logging import log, log_exception
from ..platform.client import PlatformClient, PlatformError
from . import strings as S
from .api import WhatsAppClient, WhatsAppUndeliverable, WhatsAppWindowClosed
from .keyboards import confirm_write_buttons, picker_rows, unlink_buttons
from .render import log_ref, md_to_wa, split_wa

CARD_LIMIT = 950  # interactive body hard cap is 1024


class WhatsAppProgress(Progress):
    """No edit API: every chunk is a plain send (edit_first stays False, so a
    failed first chunk is never re-sent by the placeholder fallback)."""

    def __init__(self, ch: "WhatsAppChannel", chat_id: str):
        super().__init__(chat_id, log_ref(chat_id))
        self._ch = ch

    async def _send(self, chunk: str) -> bool:
        # False = the channel swallowed it (undeliverable / window closed);
        # Progress must not count it as delivered.
        return await self._ch.send(self.chat_id, chunk)


class WhatsAppChannel:
    S = S

    def __init__(self, wa: WhatsAppClient, api: PlatformClient | None = None):
        self._wa = wa
        self._api = api
        self._unreachable: set[str] = set()

    # --- undeliverable handling --------------------------------------------

    async def _guard(self, chat_id: str, thunk) -> bool:
        """Run one send; on 131026 mark the chat blocked (once) and go quiet —
        WhatsApp's only blocked signal is a failed send. Returns False when
        the send was swallowed so callers never count it as delivered."""
        if chat_id in self._unreachable:
            return False
        try:
            await thunk()
            return True
        except WhatsAppUndeliverable:
            log("whatsapp_undeliverable", chat_id=log_ref(chat_id))
            self._unreachable.add(chat_id)
            if self._api is not None:
                try:
                    await self._api.mark_blocked(chat_id)
                except PlatformError:
                    pass
            return False
        except WhatsAppWindowClosed as exc:
            # Only reachable on Meta's extreme retry tail; nothing can be sent
            # without a paid template, so log and move on.
            log_exception("whatsapp_window_closed", exc, chat_id=log_ref(chat_id))
            return False

    # --- Channel protocol ---------------------------------------------------

    async def ack(self, ctx: Inbound) -> None:
        # Read receipt + typing indicator on everything we're about to answer.
        # mark_read never raises; the indicator clears when the reply lands.
        await self._wa.mark_read(ctx.event_id, typing=True)

    async def send(self, chat_id: str, text: str) -> bool:
        return await self._guard(chat_id, lambda: self._wa.send_text(chat_id, text))

    async def send_agent_picker(
        self, chat_id: str, agents: list[dict[str, Any]], page: int, title: str
    ) -> None:
        await self._guard(chat_id, lambda: self._wa.send_list(
            chat_id, title, "Choose agent", picker_rows(agents, page),
            section_title="Your agents",
        ))

    async def send_unlink_confirm(self, chat_id: str) -> None:
        await self._guard(chat_id, lambda: self._wa.send_buttons(
            chat_id, S.UNLINK_CONFIRM, unlink_buttons(),
        ))

    async def send_confirm_write(self, chat_id: str, pending: dict[str, Any]) -> None:
        await self._guard(chat_id, lambda: self._wa.send_buttons(
            chat_id, diff_card(pending), confirm_write_buttons(pending["pending_id"]),
        ))

    async def begin_progress(self, ctx: Inbound) -> WhatsAppProgress:
        # ack() already marked read + typing; there is nothing to edit later,
        # so no placeholder is sent — it would sit in the thread forever.
        return WhatsAppProgress(self, ctx.chat_id)

    def format_markdown(self, md: str) -> list[str]:
        return split_wa(md_to_wa(md))

    def format_verbatim(self, text: str) -> list[str]:
        # Vessel outputs are authored FOR WhatsApp (broker paste-ready, some
        # carry *bold* segment markers) — no fences, no re-wrapping.
        return split_wa(text)


def diff_card(pending: dict[str, Any]) -> str:
    diff = pending.get("diff") or {}
    lines = [f"⚠️ *Confirm write* — `{pending.get('tool', '')}`", ""]
    lines += diff_lines(pending)
    if diff.get("warning"):
        lines += ["", f"🚨 {diff['warning']}"]
    body = "\n".join(lines)
    # The interactive body caps at 1024 — truncate rather than lose the
    # buttons, and say so: approving a diff you can't fully see is exactly
    # the failure mode worth avoiding on a phone.
    if len(body) > CARD_LIMIT:
        body = body[:CARD_LIMIT - 60] + "\n\n_… truncated. Review this one in the web app._"
    return body
