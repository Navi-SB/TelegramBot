"""Inline keyboards for Telegram.

The codec (encode/decode/ref) lives in bot.core.codec — it is channel-neutral
and WhatsApp reuses it for interactive reply ids. Re-exported here so existing
imports keep working.
"""
from __future__ import annotations

from typing import Any

from ..core.codec import MAX_BYTES, VERSION, decode, encode, ref  # noqa: F401


def agent_picker(
    agents: list[dict[str, Any]], page: int = 0, per_page: int = 8
) -> dict[str, Any]:
    start = page * per_page
    window = agents[start:start + per_page]
    rows = [
        [{
            "text": ("● " if a.get("active") else "") + a["name"][:48],
            "callback_data": encode("a", ref(a["id"])),
        }]
        for a in window
    ]
    rows.append([{"text": "📄 Full text (no agent)", "callback_data": encode("a", "-")}])

    nav = []
    if page > 0:
        nav.append({"text": "‹ Prev", "callback_data": encode("ap", str(page - 1))})
    if start + per_page < len(agents):
        nav.append({"text": "Next ›", "callback_data": encode("ap", str(page + 1))})
    if nav:
        rows.append(nav)
    return {"inline_keyboard": rows}


def confirm_write(pending_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": encode("w", "y", pending_id)},
        {"text": "❌ Reject", "callback_data": encode("w", "n", pending_id)},
    ]]}


def confirm_unlink() -> dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "⚠️ Disconnect", "callback_data": encode("ul", "y")},
        {"text": "Cancel", "callback_data": encode("ul", "n")},
    ]]}
