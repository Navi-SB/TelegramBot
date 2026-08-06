"""The interaction codec — shared by every channel.

A tapped button or list row comes back as an opaque string; this codec is its
wire format: <ver>:<op>[:<arg>], ASCII, colon-separated. Channel-neutral by
construction — Telegram carries it in callback_data (64-byte cap, the binding
constraint), WhatsApp in interactive reply ids (256 allowed; we keep the same
64 so one ceiling holds everywhere).

The version prefix means a menu rendered by an older deployment degrades into
a readable "menu is stale" message instead of a silent no-op after a breaking
change.

Agent ids are UUIDs (36 chars), so "1:a:<uuid>" is 40 bytes and fits — but
only just, and built-in preset ids look like `type:handysize`, which contain a
colon and would break the codec outright. Ids are therefore sent through a
short opaque ref instead.
"""
from __future__ import annotations

import hashlib
from typing import Optional

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
