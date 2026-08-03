"""Inline keyboards and the callback_data codec.

callback_data is capped at 64 BYTES by Telegram, which is the whole design
constraint. Format: <ver>:<op>[:<arg>], ASCII, colon-separated.

The version prefix means a keyboard rendered by an older deployment degrades
into a readable message instead of a silent no-op after a breaking change.

Agent ids are UUIDs (36 chars), so "1:a:<uuid>" is 40 bytes and fits — but
only just, and built-in preset ids look like `type:handysize`, which contain a
colon and would break the codec outright. Ids are therefore sent through a
short opaque ref instead.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

VERSION = "1"
MAX_BYTES = 64


def ref(agent_id: str) -> str:
    """A short, stable, colon-free handle for an agent id."""
    return hashlib.blake2s(agent_id.encode(), digest_size=6).hexdigest()


def encode(op: str, *args: str) -> str:
    data = ":".join([VERSION, op, *args])
    if len(data.encode()) > MAX_BYTES:
        raise ValueError(f"callback_data too long ({len(data.encode())}B): {op}")
    return data


def decode(data: str) -> tuple[Optional[str], list[str]]:
    """(op, args), or (None, []) for anything this build doesn't understand."""
    parts = (data or "").split(":")
    if len(parts) < 2 or parts[0] != VERSION:
        return None, []
    return parts[1], parts[2:]


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
