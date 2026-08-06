"""Interactive message builders for WhatsApp.

Same codec as Telegram (bot.core.codec) carried in button/list-row ids, so
the core's _callback handler and the stale-menu degradation work unchanged.

The constraint here is the 10-row list ceiling: 7 agents per page + the
"Full text" row + up to two nav rows is exactly 10.
"""
from __future__ import annotations

from typing import Any

from ..core.codec import encode, ref

PER_PAGE = 7


def picker_rows(agents: list[dict[str, Any]], page: int = 0) -> list[dict[str, str]]:
    start = page * PER_PAGE
    window = agents[start:start + PER_PAGE]
    rows = []
    for a in window:
        title = (("● " if a.get("active") else "") + a["name"])[:24]
        row = {"id": encode("a", ref(a["id"])), "title": title}
        desc = a["name"] if len(a["name"]) > 24 else (a.get("kind") or "")
        if desc:
            row["description"] = str(desc)[:72]
        rows.append(row)
    rows.append({"id": encode("a", "-"), "title": "📄 Full text",
                 "description": "No agent formatting"})
    if page > 0:
        rows.append({"id": encode("ap", str(page - 1)), "title": "‹ Previous agents"})
    if start + PER_PAGE < len(agents):
        rows.append({"id": encode("ap", str(page + 1)), "title": "More agents ›"})
    return rows


def confirm_write_buttons(pending_id: str) -> list[tuple[str, str]]:
    return [(encode("w", "y", pending_id), "✅ Approve"),
            (encode("w", "n", pending_id), "❌ Reject")]


def unlink_buttons() -> list[tuple[str, str]]:
    return [(encode("ul", "y"), "⚠️ Disconnect"),
            (encode("ul", "n"), "Cancel")]
