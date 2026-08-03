"""Rendering: the markdown conversion, the 4096 split, and the callback codec.

These are the pieces that fail SILENTLY. A too-long callback_data makes a
button do nothing; a bad split makes Telegram reject a whole message with 400.
Neither shows up until a user hits it.
"""
import re

import pytest

from bot.telegram.html import LIMIT, SAFE, md_to_html, split_html, strip_tags
from bot.telegram.keyboards import (
    MAX_BYTES,
    agent_picker,
    confirm_write,
    decode,
    encode,
    ref,
)


# ---------------------------------------------------------------------------
# callback_data — a hard 64-byte ceiling
# ---------------------------------------------------------------------------
def test_every_callback_payload_fits_in_64_bytes():
    agents = [{"id": f"{i}-{'x' * 32}-uuid", "name": "A very long agent name" * 3}
              for i in range(20)]
    kb = agent_picker(agents, page=1)
    for row in kb["inline_keyboard"]:
        for button in row:
            assert len(button["callback_data"].encode()) <= MAX_BYTES, button

    # pending_ids are 32-hex
    for row in confirm_write("9f2c" + "a" * 28)["inline_keyboard"]:
        for button in row:
            assert len(button["callback_data"].encode()) <= MAX_BYTES


def test_builtin_ids_containing_a_colon_do_not_break_the_codec():
    """Built-in presets are id'd `type:handysize`. Sending that raw through a
    colon-delimited codec would split into the wrong fields."""
    r = ref("type:handysize")
    assert ":" not in r
    op, args = decode(encode("a", r))
    assert (op, args) == ("a", [r])


def test_refs_are_stable_and_distinct():
    assert ref("abc") == ref("abc")
    assert ref("abc") != ref("abd")


def test_a_stale_keyboard_decodes_to_nothing_rather_than_the_wrong_action():
    assert decode("0:a:whatever") == (None, [])
    assert decode("") == (None, [])
    assert decode("garbage") == (None, [])


def test_oversized_payload_is_refused_loudly():
    with pytest.raises(ValueError):
        encode("w", "y", "x" * 200)


# ---------------------------------------------------------------------------
# markdown -> Telegram HTML
# ---------------------------------------------------------------------------
def test_basic_formatting():
    assert "<b>bold</b>" in md_to_html("**bold**")
    assert "<i>italic</i>" in md_to_html("*italic*")
    assert "<code>x</code>" in md_to_html("`x`")
    assert "• item" in md_to_html("- item")
    assert '<a href="https://x.com">link</a>' in md_to_html("[link](https://x.com)")


def test_html_in_the_source_is_escaped_not_executed():
    out = md_to_html("5 < 6 & 7 > 2 <script>alert(1)</script>")
    assert "&lt;script&gt;" in out
    assert "<script>" not in out


def test_tables_become_monospace():
    """Telegram cannot render tables, and these get pasted into WhatsApp —
    monospace is the only way column alignment survives."""
    out = md_to_html("| Ship | DWT |\n|---|---|\n| MV A | 82000 |\n| MV LONGER | 63 |")
    assert "<pre>" in out
    assert "|---|" not in out  # the separator row carries no data
    body = strip_tags(out)
    assert "MV A" in body and "82000" in body


def test_code_fences_survive():
    out = md_to_html("```\nMV A - 82,000\nMV B - 63,000\n```")
    assert "<pre><code>" in out
    assert "MV A - 82,000" in strip_tags(out)


# ---------------------------------------------------------------------------
# splitting
# ---------------------------------------------------------------------------
def test_short_text_is_one_part():
    assert split_html("hello") == ["hello"]


def test_no_part_exceeds_telegrams_limit():
    text = md_to_html("\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(60)))
    for part in split_html(text):
        assert len(part) <= LIMIT


def test_splitting_loses_no_content():
    """The property that matters: every character of the user's answer arrives."""
    text = md_to_html("\n\n".join(f"Line {i} " + "x" * 200 for i in range(40)))
    joined = "".join(strip_tags(p) for p in split_html(text))
    # part markers are added per-part; compare on the content only
    joined = re.sub(r"\(\d+/\d+\)", "", joined)
    original = strip_tags(text)
    assert original.replace("\n", "").replace(" ", "") in joined.replace("\n", "").replace(" ", "")


def test_open_tags_are_closed_and_reopened_across_a_boundary():
    """An unbalanced tag makes Telegram reject the WHOLE message with 400."""
    text = "<pre>" + ("data line here\n" * 500) + "</pre>"
    parts = split_html(text)
    assert len(parts) > 1
    for part in parts:
        assert part.count("<pre>") == part.count("</pre>"), part[:80]


def test_a_tag_is_never_cut_in_half():
    text = "".join(f"<b>chunk {i}</b> " + "y" * 100 for i in range(80))
    for part in split_html(text):
        assert not re.search(r"<[a-zA-Z/]*$", part)
        assert not re.match(r"^[a-zA-Z/]*>", part)


def test_parts_are_numbered_so_a_reader_knows_more_is_coming():
    parts = split_html(md_to_html("word " * 3000))
    assert len(parts) > 1
    assert "(1/" in parts[0]
