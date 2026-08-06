"""Markdown -> WhatsApp markup, and fence-safe splitting at 4096.

WhatsApp's formatting is a tiny closed set: *bold*, _italic_, ~strike~,
```monospace``` and `inline code`. There is no HTML, no hyperlinked text and
no tables — tables become column-aligned monospace inside a fence, same
rationale as Telegram's <pre>: alignment is the only thing that survives a
phone screen.

Nothing here needs escaping: WhatsApp has no entity parser to reject a
message, so the worst a stray marker can do is cosmetic.
"""
from __future__ import annotations

import hashlib
import os
import re

LIMIT = 4096
# Headroom for a reopened fence and a " (2/3)" suffix.
SAFE = 3800


def log_ref(value: str) -> str:
    """WhatsApp chat ids are phone numbers — PII. Logs carry this instead.

    Keyed with BOT_INTERNAL_SECRET: phone numbers are a low-entropy space, so
    an unkeyed 4-byte hash could be inverted by enumeration by anyone holding
    the logs. With the key, the mapping cannot be recomputed from logs alone.
    (Unkeyed fallback exists only for the test environment.)"""
    secret = os.environ.get("BOT_INTERNAL_SECRET", "")
    key = hashlib.blake2s(secret.encode()).digest()[:16] if secret else b""
    return hashlib.blake2s(value.encode(), key=key, digest_size=8).hexdigest()


def md_to_wa(md: str) -> str:
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    in_fence = False
    fence_buf: list[str] = []
    table_buf: list[str] = []

    def flush_table():
        if not table_buf:
            return
        # Markdown's delimiter row is only ever the SECOND line of a table —
        # filtering every row by shape also deleted data rows like '| - | - |'
        # ('-' as empty-value marker is common in shipping tables).
        sep = re.compile(r"\s*\|?[\s:\-|]+\|?\s*")
        rows = [
            [c.strip() for c in r.strip().strip("|").split("|")]
            for i, r in enumerate(table_buf)
            if not (i == 1 and sep.fullmatch(r))
        ]
        table_buf.clear()
        if not rows:
            return
        widths = [
            max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(max(map(len, rows)))
        ]
        lines = [
            "  ".join(
                (r[i] if i < len(r) else "").ljust(widths[i]) for i in range(len(widths))
            ).rstrip()
            for r in rows
        ]
        out.append("```\n" + "\n".join(lines) + "\n```")

    for line in md.split("\n"):
        if line.strip().startswith("```"):
            flush_table()  # a buffered table must not teleport across the fence
            if in_fence:
                out.append("```\n" + "\n".join(fence_buf) + "\n```")
                fence_buf.clear()
            in_fence = not in_fence
            continue
        if in_fence:
            fence_buf.append(line)
            continue
        if "|" in line and line.strip().startswith("|"):
            table_buf.append(line)
            continue
        flush_table()
        out.append(_inline(line))

    if in_fence and fence_buf:
        out.append("```\n" + "\n".join(fence_buf) + "\n```")
    flush_table()
    return "\n".join(out).strip()


def _inline(s: str) -> str:
    # Literal \x00/\x01 in the input would collide with the mask placeholders
    # below (worst case IndexError, silently-wrong span otherwise). Control
    # chars have no legitimate rendering — drop them first.
    s = s.replace("\x00", "").replace("\x01", "")
    # Mask code spans first so no other rule chews their contents (the same
    # defect NAV-37 pinned on the Telegram renderer).
    spans: list[str] = []

    def _mask(m, _spans=spans):
        _spans.append(m.group(0))  # backticks render as inline code on WhatsApp
        return f"\x00{len(_spans) - 1}\x00"

    s = re.sub(r"`([^`]+)`", _mask, s)

    # Bold before italic, through placeholders — converting **b** to *b* first
    # would hand the italic rule its own output.
    bolds: list[str] = []

    def _bold(m, _bolds=bolds):
        _bolds.append(m.group(1))
        return f"\x01{len(_bolds) - 1}\x01"

    s = re.sub(r"\*\*([^*]+)\*\*", _bold, s)
    s = re.sub(r"__([^_]+)__", _bold, s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"_\1_", s)
    s = re.sub(r"~~([^~]+)~~", r"~\1~", s)
    # No hyperlinked text exists — show "label (url)"; bare URLs auto-link.
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", s)
    s = re.sub(r"^#{1,6}\s+(.*)$", r"*\1*", s)
    s = re.sub(r"^\s*[-*+]\s+", "• ", s)
    s = re.sub(r"\x01(\d+)\x01", lambda m: f"*{bolds[int(m.group(1))]}*", s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], s)
    return s


def split_wa(text: str, limit: int = SAFE) -> list[str]:
    """Split into WhatsApp-sized parts without ever leaving a fence open.

    An unclosed ``` renders as literal backticks, so a fence that straddles a
    boundary is closed at the cut and reopened in the next part — the one-flag
    analogue of the Telegram splitter's tag stack.
    """
    if len(text) <= limit:
        return [text] if text else []

    parts: list[str] = []
    cur: list[str] = []
    cur_len = 0
    in_fence = False
    has_content = False  # fence markers alone must never become a part

    def flush():
        nonlocal cur, cur_len, has_content
        if has_content:
            if in_fence:
                cur.append("```")
            parts.append("\n".join(cur))
        cur = ["```"] if in_fence else []
        cur_len = 4 if in_fence else 0
        has_content = False

    for line in text.split("\n"):
        pieces = [line] if len(line) <= limit else _wrap_line(line, limit)
        for piece in pieces:
            # +1 for the joining newline, +4 for a possible closing fence.
            if cur and cur_len + len(piece) + 1 + (4 if in_fence else 0) > limit:
                flush()
            cur.append(piece)
            cur_len += len(piece) + 1
            st = piece.strip()
            # A fence line toggles only when its backtick-triple count is odd:
            # balanced inline monospace at line start (WhatsApp's own syntax,
            # common in verbatim vessel output) is NOT a fence.
            if st == "```" or (st.startswith("```") and st.count("```") % 2 == 1
                               and not in_fence):
                in_fence = not in_fence
            elif st:
                has_content = True
    flush()

    if len(parts) > 1:
        parts = [f"{p}\n\n({i + 1}/{len(parts)})" for i, p in enumerate(parts)]
    return parts


def _wrap_line(line: str, size: int) -> list[str]:
    """Break one overlong line on the friendliest boundary available. Keeps
    every character: ''.join(_wrap_line(l, n)) == l."""
    out: list[str] = []
    while len(line) > size:
        cut = line.rfind(" ", size // 2, size)
        if cut <= 0:
            cut = size
        out.append(line[:cut])
        line = line[cut:]
    if line:
        out.append(line)
    return out
