"""NAV-37 — the defects that block shipping, pinned.

Every fix here guards the same invariant: the vessel rows a broker copies out
of this bot are complete and correct, and a delivery hiccup never silently
costs part of an answer.
"""
import re

import pytest

from bot.telegram.html import _wrap, esc, md_to_html, split_html, strip_tags


# ---------------------------------------------------------------------------
# _wrap must never delete a separator (the 39-rows-in, 38-rows-out weld)
# ---------------------------------------------------------------------------
def test_wrap_reassembles_to_the_exact_input():
    text = "\n".join(f"ROW{i:02d} " + "x" * 140 for i in range(39))
    for size in (150, 500, 1000, 3778):
        assert "".join(_wrap(text, size)) == text, f"size={size}"


def test_wrap_preserves_paragraph_breaks_and_spaces():
    text = ("alpha " * 100).strip() + "\n\n" + ("beta " * 100).strip()
    for size in (80, 300, 599):
        assert "".join(_wrap(text, size)) == text, f"size={size}"


def test_two_paragraphs_sharing_a_message_keep_their_boundary():
    """A cut at a paragraph boundary whose halves BOTH fit in one message used
    to weld them: the separator was stripped and the pieces re-joined with
    nothing between them. The geometry that triggers it: a <pre> block just
    over the wrap size (so it IS cut) whose pieces still fit one part, plus a
    tail that pushes the whole text over the split threshold."""
    block = "A" * 1900 + "\n\n" + "B" * 1883
    text = "<pre>" + block + "</pre>\n" + "tail " * 300
    parts = split_html(text)
    joined = "".join(strip_tags(p) for p in parts)
    assert joined.count("A") == 1900
    assert joined.count("B") == 1883
    assert "AB" not in joined  # the boundary survives wherever the cut lands


def test_csv_rows_are_never_welded_across_a_wrap():
    """Two long rows split at their shared newline, both landing in the same
    message: the newline must arrive with them."""
    row1 = "ROWONE " + "x" * 1993   # cut lands at the \n between the rows
    row2 = "ROWTWO " + "y" * 1774
    text = "<pre>" + esc(row1 + "\n" + row2) + "</pre>\n" + "tail " * 300
    parts = split_html(text)
    for part in parts:
        plain = re.sub(r"\(\d+/\d+\)", "", strip_tags(part))
        for line in plain.splitlines():
            assert not ("ROWONE" in line and "ROWTWO" in line), line[:80]
    joined = "".join(strip_tags(p) for p in parts)
    assert "ROWONE" in joined and "ROWTWO" in joined
    assert joined.count("x") == 1993 and joined.count("y") == 1774


# ---------------------------------------------------------------------------
# md_to_html must not chew on its own output (code spans vs inline rules)
# ---------------------------------------------------------------------------
def _balanced(out: str) -> bool:
    stack: list[str] = []
    for m in re.finditer(r"</?([a-z-]+)>", out):
        if m.group(0).startswith("</"):
            if not stack or stack[-1] != m.group(1):
                return False
            stack.pop()
        else:
            stack.append(m.group(1))
    return not stack


def test_a_star_inside_a_code_span_cannot_pair_with_one_outside():
    out = md_to_html("`**` bold **real**")
    assert "<code>**</code>" in out
    assert "<b>real</b>" in out
    assert _balanced(out)


def test_code_span_contents_are_left_alone():
    out = md_to_html("use `a * b` then *emphasis*")
    assert "<code>a * b</code>" in out
    assert "<i>emphasis</i>" in out
    assert _balanced(out)


def test_backtick_and_asterisk_replies_always_produce_nested_tags():
    """The failure NAV-37 describes: an ordinary reply mixing backticks and
    asterisks rendered as HTML with tags interleaved across <code> boundaries,
    which Telegram rejects wholesale."""
    samples = [
        "`*` marks a footnote, *see below*",
        "run `cmd **flag**` and **note** the output",
        "`x**y` times `z**w` is **big**",
    ]
    for s in samples:
        assert _balanced(md_to_html(s)), s


# ---------------------------------------------------------------------------
# delivery: one failed send must never cost the rest of the answer
# ---------------------------------------------------------------------------
from bot.dispatch import handle_update  # noqa: E402
from bot.telegram.api import TelegramError  # noqa: E402
from tests.test_dispatch import FakeApi, FakeTg, msg  # noqa: E402


class FlakyTg(FakeTg):
    """FakeTg where chosen send_safe calls (1-indexed) or every edit fail."""

    def __init__(self, *, fail_sends=(), fail_edits=False):
        super().__init__()
        self._send_calls = 0
        self._fail_sends = set(fail_sends)
        self._fail_edits = fail_edits

    async def send_safe(self, chat_id, text, *, reply_markup=None):
        self._send_calls += 1
        if self._send_calls in self._fail_sends:
            raise TelegramError(400, "Bad Request: chat temporarily unavailable")
        return await super().send_safe(chat_id, text, reply_markup=reply_markup)

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        if self._fail_edits:
            raise TelegramError(400, "Bad Request: message to edit not found")
        await super().edit_message_text(chat_id, message_id, text, **kw)


def _turn_result(**over):
    base = {"reply": "", "vessel_outputs": [], "pending": [], "tools_used": [],
            "stop_reason": "end_turn", "error": None}
    return {**base, **over}


@pytest.mark.asyncio
async def test_one_failed_send_does_not_truncate_the_fleet_listing():
    api = FakeApi(turn=_turn_result(vessel_outputs=["OUT-ONE", "OUT-TWO", "OUT-THREE"]))
    # send #1 is the placeholder; OUT-ONE replaces it via edit;
    # OUT-TWO is send #2 — make exactly that one fail.
    tg = FlakyTg(fail_sends={2})
    await handle_update(msg("fleet list"), tg, api, turn_timeout=30)
    assert "OUT-ONE" in tg.all_text
    assert "OUT-TWO" not in tg.all_text  # lost to the (logged) error
    assert "OUT-THREE" in tg.all_text   # but the rest still arrived


@pytest.mark.asyncio
async def test_a_failed_placeholder_edit_does_not_discard_the_turn():
    """The old behaviour: the edit raised, handle_update's boundary swallowed
    the turn, and '⏳ Thinking…' sat on screen forever."""
    api = FakeApi(turn=_turn_result(reply="You have 12 Panamaxes."))
    tg = FlakyTg(fail_edits=True)
    await handle_update(msg("how many?"), tg, api, turn_timeout=30)
    assert any("12 Panamaxes" in t for _, t in tg.sent)  # arrived as a plain send


@pytest.mark.asyncio
async def test_a_failed_confirm_card_does_not_take_the_others_down():
    api = FakeApi(turn=_turn_result(
        reply="done",
        pending=[{"pending_id": "p1", "tool": "add_fixtures", "diff": {}},
                 {"pending_id": "p2", "tool": "add_fixtures", "diff": {}}]))
    # sends: #1 placeholder, reply edits it; card p1 is send #2 — fail it.
    tg = FlakyTg(fail_sends={2})
    await handle_update(msg("log these"), tg, api, turn_timeout=30)
    assert len(tg.keyboards) == 1  # p2's buttons still went out
