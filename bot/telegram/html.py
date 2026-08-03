"""Markdown -> Telegram HTML, and entity-safe splitting at 4096.

WHY HTML AND NOT MARKDOWNV2

MarkdownV2 requires escaping eighteen characters — _ * [ ] ( ) ~ ` > # + - =
| { } . ! — EVERYWHERE, and agent output is full of them: DWT figures like
52.000, hyphenated vessel names, parenthesised ports, and the pipe tables the
system prompt asks for in table mode. One missed escape and Telegram rejects
the WHOLE message with 400 "can't parse entities".

HTML needs three escapes and a small closed tag set. It is also the only form
that can be split safely: you can track a stack of open tags and reopen them
across a boundary. With MarkdownV2 there is no way to resume mid-escape-run.
"""
from __future__ import annotations

import html as _html
import re

LIMIT = 4096
# Headroom for reopened tags and a " (2/3)" suffix.
SAFE = 3800

_ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}
_TAG_RE = re.compile(r"</?([a-zA-Z0-9-]+)(\s[^>]*)?>")
_TOKEN_RE = re.compile(r"(</?[a-zA-Z0-9-]+(?:\s[^>]*)?>|&[a-zA-Z]+;|&#\d+;)")


def esc(text: str) -> str:
    return _html.escape(text, quote=False)


def strip_tags(text: str) -> str:
    """Plain-text fallback: drop markup, keep every character of content."""
    return _html.unescape(_TAG_RE.sub("", text))


def md_to_html(md: str) -> str:
    """Convert the markdown the agent emits into Telegram's HTML subset.

    Tables become <pre>: Telegram cannot render them, and monospace is the only
    way column alignment survives — which matters because these get pasted
    into WhatsApp.
    """
    out: list[str] = []
    in_fence = False
    fence_buf: list[str] = []
    table_buf: list[str] = []

    def flush_table():
        if not table_buf:
            return
        rows = [
            [c.strip() for c in r.strip().strip("|").split("|")]
            for r in table_buf
            # the |---|---| separator row carries no data
            if not re.fullmatch(r"\s*\|?[\s:\-|]+\|?\s*", r)
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
        out.append("<pre>" + esc("\n".join(lines)) + "</pre>")

    for line in md.split("\n"):
        if line.strip().startswith("```"):
            if in_fence:
                out.append("<pre><code>" + esc("\n".join(fence_buf)) + "</code></pre>")
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

        s = esc(line)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"__([^_]+)__", r"<b>\1</b>", s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"^#{1,6}\s+(.*)$", r"<b>\1</b>", s)
        s = re.sub(r"^\s*[-*+]\s+", "• ", s)
        out.append(s)

    if in_fence and fence_buf:
        out.append("<pre><code>" + esc("\n".join(fence_buf)) + "</code></pre>")
    flush_table()
    return "\n".join(out).strip()


def _open_tag(token: str) -> str | None:
    m = _TAG_RE.fullmatch(token)
    if not m or token.startswith("</"):
        return None
    name = m.group(1).lower()
    return name if name in _ALLOWED_TAGS else None


def _close_tag(token: str) -> str | None:
    m = _TAG_RE.fullmatch(token)
    if not m or not token.startswith("</"):
        return None
    name = m.group(1).lower()
    return name if name in _ALLOWED_TAGS else None


def split_html(text: str, limit: int = SAFE) -> list[str]:
    """Split into Telegram-sized parts without ever cutting a tag or entity.

    Open tags are closed at the boundary and reopened in the next part, so a
    long <pre> block becomes two valid blocks rather than one broken one.
    """
    if len(text) <= limit:
        return [text] if text else []

    parts: list[str] = []
    stack: list[str] = []
    cur: list[str] = []
    cur_len = 0

    def closing() -> str:
        return "".join(f"</{t}>" for t in reversed(stack))

    def reopen() -> str:
        return "".join(f"<{t}>" for t in stack)

    def flush():
        nonlocal cur, cur_len
        if cur:
            parts.append("".join(cur) + closing())
        cur = [reopen()] if stack else []
        cur_len = len("".join(cur))

    for token in _TOKEN_RE.split(text):
        if not token:
            continue
        is_markup = bool(_TOKEN_RE.fullmatch(token))
        pieces = [token] if is_markup else _wrap(token, limit - len(closing()) - 16)

        for piece in pieces:
            if cur_len + len(piece) + len(closing()) > limit and cur_len > 0:
                flush()
            cur.append(piece)
            cur_len += len(piece)
            if is_markup:
                if (o := _open_tag(piece)):
                    stack.append(o)
                elif (c := _close_tag(piece)) and stack and stack[-1] == c:
                    stack.pop()
    flush()

    out = [p for p in parts if strip_tags(p).strip()]
    if len(out) > 1:
        out = [f"{p}\n\n<i>({i + 1}/{len(out)})</i>" for i, p in enumerate(out)]
    return out


def _wrap(text: str, size: int) -> list[str]:
    """Break a long run of plain text on the friendliest boundary available:
    paragraph, then line, then word, then a hard cut."""
    if size <= 0 or len(text) <= size:
        return [text]
    out: list[str] = []
    while len(text) > size:
        window = text[:size]
        cut = window.rfind("\n\n")
        if cut < size // 2:
            cut = window.rfind("\n")
        if cut < size // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out
