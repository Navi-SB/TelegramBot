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


# ---------------------------------------------------------------------------
# the confirm card must show WHAT is about to change
# ---------------------------------------------------------------------------
from bot.dispatch import _diff_card  # noqa: E402
from bot.platform.client import PlatformError  # noqa: E402
from bot.telegram.keyboards import encode  # noqa: E402
from tests.test_dispatch import cb  # noqa: E402


def test_an_update_diff_shows_field_from_to():
    card = _diff_card({"pending_id": "p1", "tool": "update_vessel",
                       "diff": {"before": {"dwt": 82000}, "after": {"dwt": 81000},
                                "changed": {"dwt": {"from": 82000, "to": 81000}}}})
    assert "dwt: 82000 → 81000" in card
    assert card.count("update_vessel") == 1  # the tool name, once, in the header


def test_an_add_vessel_diff_lists_the_new_record():
    card = _diff_card({"pending_id": "p1", "tool": "add_vessel",
                       "diff": {"before": None,
                                "after": {"name": "MV OCEAN STAR", "dwt": 82000,
                                          "draft": None}}})
    assert "name: MV OCEAN STAR" in card
    assert "dwt: 82000" in card
    assert "draft" not in card  # nulls are noise on a phone


def test_an_add_fixtures_diff_shows_count_and_rows():
    card = _diff_card({"pending_id": "p1", "tool": "add_fixtures",
                       "diff": {"before": None,
                                "after": {"count": 2, "fixtures": [
                                    {"vessel": "MV A", "rate": 14.5},
                                    {"vessel": "MV B", "rate": 12.0}]}}})
    assert "Adding 2 item(s):" in card
    assert "vessel: MV A" in card and "vessel: MV B" in card
    assert card.count("add_fixtures") == 1


def test_a_remove_fixtures_diff_says_what_goes():
    card = _diff_card({"pending_id": "p1", "tool": "remove_fixtures",
                       "diff": {"before": {"count": 1, "fixtures": [{"vessel": "MV A"}]},
                                "after": None}})
    assert "Removing 1 item(s):" in card
    assert "vessel: MV A" in card


def test_a_diff_warning_is_surfaced():
    card = _diff_card({"pending_id": "p1", "tool": "set_columns",
                       "diff": {"before": None, "after": {"cols": "x"},
                                "warning": "This clears 3 saved charts."}})
    assert "🚨" in card and "This clears 3 saved charts." in card


# ---------------------------------------------------------------------------
# a 409 is only a double tap when the server SAYS so
# ---------------------------------------------------------------------------
class Api409(FakeApi):
    def __init__(self, detail):
        super().__init__()
        self._detail = detail

    async def confirm(self, chat_id, pending_id, action):
        raise PlatformError(409, self._detail)


@pytest.mark.asyncio
async def test_a_real_apply_failure_is_not_reported_as_already_resolved():
    """apply_pending hands the row back and raises for 'Vessel X not found.' —
    a retryable failure the user must hear about, not a double tap."""
    tg = FakeTg()
    await handle_update(cb(encode("w", "y", "p1")), tg,
                        Api409("Vessel 'MV GHOST' not found."), turn_timeout=30)
    assert "Already resolved" not in tg.all_text
    assert "couldn't be applied" in tg.all_text
    assert "MV GHOST" in tg.all_text  # the server's sentence, shown


@pytest.mark.asyncio
async def test_a_status_conflict_409_still_reads_as_success():
    tg = FakeTg()
    await handle_update(cb(encode("w", "y", "p1")), tg,
                        Api409("Pending write p1 is approved, not pending."),
                        turn_timeout=30)
    assert "Already resolved" in tg.all_text


# ---------------------------------------------------------------------------
# review findings, pinned (Telegram side)
# ---------------------------------------------------------------------------
def test_placeholder_shaped_input_does_not_crash_md_to_html():
    out = md_to_html("before \x007\x00 after")
    assert "7" in strip_tags(out)
    out = md_to_html("has `code` and stray \x000\x00 too")
    assert "<code>code</code>" in out


def test_a_table_does_not_teleport_across_a_fence_html():
    out = md_to_html("| a | b |\n|---|---|\n| 1 | 2 |\n```\ncode here\n```\ntail")
    assert out.index("a  b") < out.index("code here") < out.index("tail")


def test_all_dash_data_rows_survive_html():
    out = strip_tags(md_to_html("| item | val |\n|---|---|\n| - | - |\n| x | 1 |"))
    assert re.search(r"^-\s+-$", out, re.M), out


@pytest.mark.asyncio
async def test_agent_no_match_escapes_the_query():
    """Declared behavior change (found in review): the old raw interpolation
    made Telegram reject the message and the strip_tags fallback then ATE the
    user's query. Escaping keeps it visible."""
    api = FakeApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    tg = FakeTg()
    await handle_update(msg("/agent <handysize>"), tg, api, turn_timeout=30)
    assert "No agent matches <b>&lt;handysize&gt;</b>." in tg.all_text


@pytest.mark.asyncio
async def test_short_opens_the_format_picker_on_telegram():
    """/short shows only the formats — no 'Full text' row — and selection
    rides the same ref codec (colon-heavy sentinel ids are what it's for)."""
    class ToolsApi(FakeApi):
        async def agents(self, chat_id):
            res = await super().agents(chat_id)
            return {**res, "tools": [{"id": "tool:shortdesc:geared",
                                      "name": "Short desc · Geared",
                                      "kind": "tool"}]}

    api = ToolsApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    tg = FakeTg()
    await handle_update(msg("/short"), tg, api, turn_timeout=30)
    buttons = [b for row in tg.keyboards[0]["inline_keyboard"] for b in row]
    texts = [b["text"] for b in buttons]
    assert texts == ["Short desc · Geared"]  # no Full text, no agents
    assert len(buttons[0]["callback_data"].encode()) <= 64

    await handle_update(cb(buttons[0]["callback_data"]), tg, api, turn_timeout=30)
    assert api.selected == "tool:shortdesc:geared"
    assert "paste a full vessel description" in tg.all_text.lower()
