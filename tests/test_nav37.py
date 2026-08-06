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
