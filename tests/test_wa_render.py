"""WhatsApp rendering: markdown conversion, the 4096 split, interactive
builders. Same failure modes as the Telegram equivalents — silent until a
user hits them."""
import re

import pytest

from bot.whatsapp.keyboards import confirm_write_buttons, picker_rows, unlink_buttons
from bot.whatsapp.render import LIMIT, md_to_wa, split_wa
from bot.core.codec import decode


# ---------------------------------------------------------------------------
# markdown -> WhatsApp markup
# ---------------------------------------------------------------------------
def test_basic_formatting():
    assert md_to_wa("**bold**") == "*bold*"
    assert md_to_wa("*italic*") == "_italic_"
    assert md_to_wa("~~gone~~") == "~gone~"
    assert md_to_wa("# Heading") == "*Heading*"
    assert md_to_wa("- item") == "• item"


def test_bold_conversion_does_not_feed_the_italic_rule():
    """**b** must become *b* — not *b* then _b_. The bold output is exactly
    what the italic rule matches, so it hides behind placeholders."""
    assert md_to_wa("**b** and *i*") == "*b* and _i_"
    assert md_to_wa("__also bold__") == "*also bold*"


def test_links_become_label_then_url():
    assert md_to_wa("[Tropis](https://tropishq.com)") == "Tropis (https://tropishq.com)"


def test_code_spans_are_left_alone():
    assert md_to_wa("`a * b` and *i*") == "`a * b` and _i_"
    assert md_to_wa("run `cmd **flag**` now") == "run `cmd **flag**` now"


def test_tables_become_fenced_monospace():
    out = md_to_wa("| Ship | DWT |\n|---|---|\n| MV A | 82000 |\n| MV LONGER | 63 |")
    assert out.startswith("```")
    assert out.endswith("```")
    assert "|---|" not in out
    assert "MV A" in out and "82000" in out


def test_code_fences_survive():
    out = md_to_wa("```\nMV A - 82,000\nMV B - 63,000\n```")
    assert out.count("```") == 2
    assert "MV A - 82,000" in out


# ---------------------------------------------------------------------------
# splitting
# ---------------------------------------------------------------------------
def test_short_text_is_one_part():
    assert split_wa("hello") == ["hello"]
    assert split_wa("") == []


def test_no_part_exceeds_whatsapps_limit():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(60))
    parts = split_wa(text)
    assert len(parts) > 1
    for part in parts:
        assert len(part) <= LIMIT


def test_splitting_loses_no_content():
    text = "\n".join(f"ROW{i:03d} " + "x" * 150 for i in range(80))
    parts = split_wa(text)
    joined = "\n".join(re.sub(r"\n\n\(\d+/\d+\)$", "", p) for p in parts)
    for i in range(80):
        assert f"ROW{i:03d}" in joined
    assert joined.count("x") == 80 * 150


def test_rows_are_never_welded_across_a_split():
    text = "\n".join(f"ROW{i:03d} " + "x" * 150 for i in range(80))
    for part in split_wa(text):
        for line in part.splitlines():
            assert line.count("ROW") <= 1, line[:60]


def test_fences_are_balanced_in_every_part():
    """An unclosed ``` renders as literal backticks — every part must close
    what it opens, and a straddling fence reopens in the next part."""
    text = "```\n" + ("data line here\n" * 600).strip() + "\n```"
    parts = split_wa(text)
    assert len(parts) > 1
    for part in parts:
        body = re.sub(r"\n\n\(\d+/\d+\)$", "", part)
        assert body.count("```") % 2 == 0, part[:80]


def test_a_single_overlong_line_is_wrapped_not_dropped():
    text = "word " * 2000  # one line, ~10000 chars
    parts = split_wa(text.strip())
    assert len(parts) > 1
    joined = "".join(re.sub(r"\n\n\(\d+/\d+\)$", "", p).replace("\n", " ") for p in parts)
    assert joined.count("word") == 2000


def test_parts_are_numbered_so_a_reader_knows_more_is_coming():
    parts = split_wa("word " * 3000)
    assert len(parts) > 1
    assert parts[0].endswith(f"(1/{len(parts)})")


# ---------------------------------------------------------------------------
# interactive builders — the 10-row list ceiling is the hard constraint
# ---------------------------------------------------------------------------
def _agents(n):
    return [{"id": f"agent-{i}-{'x' * 30}", "name": f"Agent Number {i} With A Long Name",
             "kind": "template"} for i in range(n)]


@pytest.mark.parametrize("n", [1, 7, 8, 20, 50])
def test_picker_never_exceeds_ten_rows(n):
    agents = _agents(n)
    pages = (n + 6) // 7
    for page in range(pages):
        rows = picker_rows(agents, page)
        assert len(rows) <= 10, f"{n} agents, page {page}: {len(rows)} rows"
        for row in rows:
            assert len(row["title"]) <= 24
            assert len(row["id"].encode()) <= 64
            if "description" in row:
                assert len(row["description"]) <= 72


def test_every_agent_is_reachable_across_pages():
    agents = _agents(20)
    seen = set()
    page = 0
    while True:
        rows = picker_rows(agents, page)
        seen |= {r["id"] for r in rows if decode(r["id"])[0] == "a" and decode(r["id"])[1] != ["-"]}
        if not any(decode(r["id"]) == ("ap", [str(page + 1)]) for r in rows):
            break
        page += 1
    assert len(seen) == 20


def test_picker_always_offers_full_text():
    rows = picker_rows(_agents(3))
    assert any(decode(r["id"]) == ("a", ["-"]) for r in rows)


def test_button_titles_fit_whatsapps_20_char_cap():
    for bid, title in confirm_write_buttons("9f2c" + "a" * 28) + unlink_buttons():
        assert len(title) <= 20
        assert len(bid.encode()) <= 64


# ---------------------------------------------------------------------------
# review findings, pinned
# ---------------------------------------------------------------------------
def test_a_table_does_not_teleport_across_a_fence():
    out = md_to_wa("| a | b |\n|---|---|\n| 1 | 2 |\n```\ncode here\n```\ntail")
    assert out.index("a  b") < out.index("code here") < out.index("tail")


def test_two_tables_split_by_a_fence_stay_two_tables():
    out = md_to_wa("| a |\n|---|\n| 1 |\n```\nX\n```\n| b |\n|---|\n| 2 |")
    assert out.count("```") == 6  # table, fence, table — three fenced blocks


def test_all_dash_data_rows_survive():
    """'-' as empty-value marker is common in shipping tables; only the
    delimiter row (line 2) carries no data."""
    out = md_to_wa("| item | val |\n|---|---|\n| - | - |\n| x | 1 |")
    assert re.search(r"^-\s+-$", out, re.M), out  # the dash row, column-aligned
    assert "|---|" not in out


def test_placeholder_shaped_input_does_not_crash_or_corrupt():
    assert md_to_wa("before \x007\x00 after") == "before 7 after"
    assert md_to_wa("before \x011\x01 after") == "before 1 after"
    out = md_to_wa("has `code` and stray \x000\x00 too")
    assert "`code`" in out and "stray 0 too" in out


def test_no_part_is_only_fence_markers():
    parts = split_wa(md_to_wa("```\n" + "B" * 9000 + "\n```"))
    for part in parts:
        body = re.sub(r"\n\n\(\d+/\d+\)$", "", part)
        assert body.replace("```", "").strip(), repr(part[:40])
    # trailing variant
    lines = ["x" * 100] * 74 + ["y" * 3790]
    for part in split_wa("```\n" + "\n".join(lines) + "\n```"):
        body = re.sub(r"\n\n\(\d+/\d+\)$", "", part)
        assert body.replace("```", "").strip(), repr(part[:40])


def test_inline_monospace_at_line_start_is_not_a_fence():
    """Vessel outputs are WhatsApp-native and may open with balanced inline
    monospace; that must not flip the splitter into fence mode."""
    text = "```MV OCEAN GLORY``` - 82,000 DWT open Singapore\n" + \
        "\n".join(f"line {i} " + "d" * 120 for i in range(80))
    parts = split_wa(text, limit=800)
    for part in parts:
        for line in part.splitlines():
            assert line.strip() != "```", part[:80]


def test_log_ref_is_keyed_by_the_internal_secret(monkeypatch):
    from bot.whatsapp.render import log_ref
    monkeypatch.setenv("BOT_INTERNAL_SECRET", "secret-a")
    a = log_ref("306912345678")
    monkeypatch.setenv("BOT_INTERNAL_SECRET", "secret-b")
    b = log_ref("306912345678")
    assert a != b                       # not recomputable without the key
    assert "306912345678" not in a
