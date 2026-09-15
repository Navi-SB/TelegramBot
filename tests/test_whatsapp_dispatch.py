"""What the WhatsApp bot actually does — the core flows driven through the
WhatsApp channel adapter, with a fake Cloud API client and the same fake
platform the Telegram tests use. The behavioural invariants (reveal-nothing
gate, outputs-before-error, 409 semantics) must hold identically here."""
import pytest

from bot.core.codec import encode, ref
from bot.core.dispatch import handle_inbound
from bot.platform.client import PlatformError, PlatformUnavailable
from bot.whatsapp import strings as S
from bot.whatsapp.api import WhatsAppError, WhatsAppUndeliverable, WhatsAppWindowClosed
from bot.whatsapp.channel import WhatsAppChannel
from bot.whatsapp.parse import parse_envelopes
from tests.test_dispatch import FakeApi
from tests.test_wa_parse import NUM, text_msg, wa_body


class FakeWa:
    """Records every Cloud API call; no edits exist to record."""

    def __init__(self):
        self.texts: list[tuple[str, str]] = []
        self.buttons: list[tuple[str, str, list]] = []
        self.lists: list[tuple[str, str, list]] = []
        self.reads: list[tuple[str, bool]] = []

    async def send_text(self, to, body):
        self.texts.append((str(to), body))
        return "wamid.out"

    async def send_buttons(self, to, body, buttons):
        self.buttons.append((str(to), body, buttons))
        return "wamid.out"

    async def send_list(self, to, body, button_text, rows, section_title="Options"):
        self.lists.append((str(to), body, rows))
        return "wamid.out"

    async def mark_read(self, message_id, *, typing=False):
        self.reads.append((message_id, typing))

    @property
    def all_text(self) -> str:
        return "\n".join(b for _, b in self.texts) \
            + "\n".join(b for _, b, _ in self.buttons) \
            + "\n".join(b for _, b, _ in self.lists)


def interactive(kind, payload_id, mid="wamid.T1"):
    return {"from": NUM, "id": mid, "type": "interactive",
            "interactive": {"type": kind, kind: {"id": payload_id, "title": "t"}}}


async def run(message, api, wa=None):
    wa = wa or FakeWa()
    [ctx] = parse_envelopes(wa_body([message]))
    await handle_inbound(ctx, WhatsAppChannel(wa, api), api, turn_timeout=30)
    return wa


def turn_result(**over):
    base = {"reply": "", "vessel_outputs": [], "pending": [], "tools_used": [],
            "stop_reason": "end_turn", "error": None}
    return {**base, **over}


# ---------------------------------------------------------------------------
# pairing — LINK <code> replaces the /start deep link
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_unlinked_chat_only_ever_gets_pairing_instructions():
    api = FakeApi(linked=False)
    wa = await run(text_msg("what vessels do I have?"), api)
    assert S.NOT_LINKED in wa.all_text
    assert "turn" not in api.calls


@pytest.mark.asyncio
async def test_the_unlinked_message_reveals_nothing():
    wa = await run(text_msg("hi"), FakeApi(linked=False))
    text = wa.all_text.lower()
    for leak in ("unknown user", "not found", "no account with", NUM):
        assert leak not in text


@pytest.mark.asyncio
async def test_link_code_is_the_one_unlinked_action():
    api = FakeApi(linked=False)
    wa = await run(text_msg("LINK somecode"), api)
    assert "redeem" in api.calls
    assert "Connected to" in wa.all_text


@pytest.mark.asyncio
async def test_the_code_reaches_the_platform_verbatim():
    codes = []

    class Api(FakeApi):
        async def redeem(self, chat_id, code, user_id, name):
            codes.append(code)
            return await super().redeem(chat_id, code, user_id, name)

    await run(text_msg("link AbC_9-XyZ"), Api(linked=False))
    assert codes == ["AbC_9-XyZ"]


@pytest.mark.asyncio
async def test_a_bad_code_says_so_without_detail():
    wa = await run(text_msg("LINK bad"), FakeApi(linked=False))
    assert S.LINK_FAILED in wa.all_text


@pytest.mark.asyncio
async def test_an_unreachable_platform_is_not_reported_as_a_bad_code():
    # The backend never saw the code, so it wasn't burned — "invalid or
    # expired" would send the user off to regenerate a still-valid link.
    class Api(FakeApi):
        async def redeem(self, chat_id, code, user_id, name):
            raise PlatformUnavailable(0, "ConnectError")

    wa = await run(text_msg("LINK somecode"), Api(linked=False))
    assert S.PLATFORM_DOWN in wa.all_text
    assert S.LINK_FAILED not in wa.all_text


@pytest.mark.asyncio
async def test_pairing_that_succeeds_is_announced_even_if_the_agent_list_fails():
    class Api(FakeApi):
        async def agents(self, chat_id):
            raise PlatformUnavailable(0, "ReadTimeout")

    wa = await run(text_msg("LINK somecode"), Api(linked=False))
    assert "Connected to" in wa.all_text
    assert S.PLATFORM_DOWN in wa.all_text  # the missing picker is explained, not silence


# ---------------------------------------------------------------------------
# commands without a command menu
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_bare_agents_word_opens_the_list_picker():
    api = FakeApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    wa = await run(text_msg("agents"), api)
    assert wa.lists, "expected a list message"
    _, _, rows = wa.lists[0]
    titles = [r["title"] for r in rows]
    assert "Freight Desk" in titles
    assert any("Full text" in t for t in titles)


@pytest.mark.asyncio
async def test_the_agent_list_says_whose_agents_it_is_showing():
    api = FakeApi(agents=[{"id": "a1", "name": "Default", "kind": "template"}],
                  account="Acme Shipping")
    wa = await run(text_msg("agents"), api)
    (_, body, _), = wa.lists
    assert body == "Which agent should I use?\n_Showing the agents for Acme Shipping._"

    wa = await run(text_msg("agents"), FakeApi(agents=api._agents))
    (_, body, _), = wa.lists
    assert body == S.PICK_AGENT


@pytest.mark.asyncio
async def test_status_names_whose_agents_the_chat_uses():
    wa = await run(text_msg("status"), FakeApi(account="Acme Shipping"))
    assert "*Agents from:* Acme Shipping" in wa.texts[-1][1]

    wa = await run(text_msg("status"), FakeApi())
    assert "Agents from" not in wa.texts[-1][1]
    assert "*Account:* a@x.com" in wa.texts[-1][1]


@pytest.mark.asyncio
async def test_no_agent_match_names_the_account_it_searched():
    api = FakeApi(agents=[{"id": "a1", "name": "Default", "kind": "template"}],
                  account="alex@personal.com")
    wa = await run(text_msg("/agent PMX Short"), api)
    assert wa.texts[-1][1] == (
        "No agent matches *PMX Short* in the agents for *alex@personal.com*. "
        "Send *agents* to see the list."
    )

    wa = await run(text_msg("/agent PMX Short"), FakeApi(agents=api._agents))
    assert wa.texts[-1][1] == "No agent matches *PMX Short*. Send *agents* to see the list."


@pytest.mark.asyncio
async def test_multi_word_messages_go_to_the_agent_not_the_command_router():
    api = FakeApi()
    await run(text_msg("new fixture for MV OCEAN STAR"), api)
    assert "turn" in api.calls


@pytest.mark.asyncio
async def test_help_needs_no_network():
    api = FakeApi()
    wa = await run(text_msg("help"), api)
    assert "Tropis assistant" in wa.all_text
    assert api.calls == []


def test_help_says_how_to_switch_agents_by_name():
    """Both ways: the assistant is told /agent <name> works here, and it is
    the one the bridge reads itself, whatever the platform recognises."""
    assert "*switch to <name>*" in S.HELP
    assert "*/agent <name>*" in S.HELP


@pytest.mark.asyncio
async def test_agent_and_a_name_without_the_slash_is_a_message_not_a_command():
    """'agent confirms berthing tomorrow' is a real message in this trade.
    'switch to <name>' is recognised by the platform, inside the turn."""
    api = FakeApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    await run(text_msg("agent Freight Desk"), api)
    assert "turn" in api.calls
    assert api.selected is None


@pytest.mark.asyncio
async def test_every_confirmation_names_the_bare_word_that_opens_the_menu():
    api = FakeApi(linked=False, agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    wa = await run(text_msg("LINK somecode"), api)
    assert "*agents*" in wa.texts[0][1]

    api = FakeApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    for message in (text_msg("/agent freight desk"), text_msg("new"),
                    interactive("list_reply", encode("a", "-"))):
        wa = await run(message, api)
        assert S.SWITCH_HINT in wa.texts[-1][1]

    # ...and the word it names really is a command here.
    [ctx] = parse_envelopes(wa_body([text_msg("agents")]))
    assert ctx.command == "agents"


@pytest.mark.asyncio
async def test_media_gets_a_useful_refusal():
    msg = {"from": NUM, "id": "wamid.M1", "type": "image", "image": {"id": "m1"}}
    wa = await run(msg, FakeApi())
    assert S.TEXT_ONLY in wa.all_text


# ---------------------------------------------------------------------------
# the turn — no placeholder, no edits, buffered sends
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_turn_marks_read_with_typing_and_sends_no_placeholder():
    api = FakeApi(turn=turn_result(reply="You have 12 Panamaxes."))
    wa = await run(text_msg("how many?", mid="wamid.Q1"), api)
    assert ("wamid.Q1", True) in wa.reads      # read receipt + typing indicator
    assert "Thinking" not in wa.all_text       # a placeholder would sit forever
    assert "12 Panamaxes" in wa.texts[0][1]


@pytest.mark.asyncio
async def test_vessel_outputs_go_first_verbatim_and_unfenced():
    """These templates are authored FOR WhatsApp — some carry *bold* segment
    markers. Fencing them would suppress the intended rendering."""
    rendered = "*HANDY*\nMV OCEAN STAR - 82,000 MT DWT ON 14.5 M SSW"
    api = FakeApi(turn=turn_result(reply="Here you go.", vessel_outputs=[rendered]))
    wa = await run(text_msg("describe ocean star"), api)
    assert wa.texts[0][1] == rendered           # first, byte-for-byte, no ```
    assert "Here you go." in wa.texts[1][1]


@pytest.mark.asyncio
async def test_outputs_rendered_before_a_late_error_still_ship():
    card = "Singapore bunker prices — 2026-08-04\nVLSFO: 844.5"
    api = FakeApi(turn=turn_result(vessel_outputs=[card], stop_reason="error",
                                   error="APIError: overloaded"))
    wa = await run(text_msg("concordia with current prices"), api)
    assert "Singapore bunker prices" in wa.texts[0][1]
    assert "APIError" in wa.texts[-1][1]
    assert "overloaded" not in wa.all_text      # class only, no detail


@pytest.mark.asyncio
async def test_a_write_proposal_becomes_reply_buttons():
    api = FakeApi(turn=turn_result(
        stop_reason="needs_confirmation",
        pending=[{"pending_id": "p1", "tool": "add_fixtures",
                  "diff": {"before": None,
                           "after": {"count": 1, "fixtures": [{"vessel": "MV A"}]}}}]))
    wa = await run(text_msg("log these fixtures"), api)
    (_, body, buttons), = wa.buttons
    titles = [t for _, t in buttons]
    assert "✅ Approve" in titles and "❌ Reject" in titles
    assert "vessel: MV A" in body               # the card shows the actual diff
    ids = [i for i, _ in buttons]
    assert encode("w", "y", "p1") in ids


@pytest.mark.asyncio
async def test_a_turn_that_asks_for_the_agent_menu_sends_the_list_after_the_reply():
    """Same flow as Telegram through the channel abstraction: the reply goes
    out as text, then the list message the user taps to switch."""
    api = FakeApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}],
                  turn=turn_result(reply="No agent called PMX here.",
                                   stop_reason="agent_menu", menu="agents"))
    wa = await run(text_msg("switch to PMX"), api)
    assert "No agent called PMX here." in wa.texts[0][1]
    (_, body, rows), = wa.lists
    assert body == S.PICK_AGENT
    assert "Freight Desk" in [r["title"] for r in rows]


@pytest.mark.asyncio
async def test_a_turn_without_a_menu_request_sends_no_list():
    api = FakeApi(turn=turn_result(reply="You have 12 Panamaxes."))
    wa = await run(text_msg("how many panamaxes?"), api)
    assert wa.lists == []
    assert "agents" not in api.calls


# ---------------------------------------------------------------------------
# interactive replies drive the same codec paths
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_list_selection_selects_the_agent_by_ref():
    api = FakeApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    wa = await run(interactive("list_reply", encode("a", ref("a1"))), api)
    assert api.selected == "a1"
    assert "Freight Desk" in wa.all_text


@pytest.mark.asyncio
async def test_a_stale_menu_says_so():
    wa = await run(interactive("list_reply", "0:a:oldref"), FakeApi())
    assert S.STALE_MENU in wa.all_text


@pytest.mark.asyncio
async def test_a_double_tapped_approve_reads_409_as_success():
    api = FakeApi(fail="already")
    wa = await run(interactive("button_reply", encode("w", "y", "p1")), api)
    assert S.ALREADY_RESOLVED in wa.all_text


@pytest.mark.asyncio
async def test_a_real_apply_failure_surfaces_instead_of_fake_success():
    class Api(FakeApi):
        async def confirm(self, chat_id, pending_id, action):
            raise PlatformError(409, "Vessel 'MV GHOST' not found.")

    wa = await run(interactive("button_reply", encode("w", "y", "p1")), Api())
    assert S.ALREADY_RESOLVED not in wa.all_text
    assert "couldn't be applied" in wa.all_text


@pytest.mark.asyncio
async def test_unlink_is_two_step():
    api = FakeApi()
    wa = await run(text_msg("unlink"), api)
    assert not api.revoked
    assert wa.buttons                            # asked, with buttons

    await run(interactive("button_reply", encode("ul", "y")), api)
    assert api.revoked


# ---------------------------------------------------------------------------
# blocked is learned send-side: 131026 → mark_blocked, once, then quiet
# ---------------------------------------------------------------------------
class UnreachableWa(FakeWa):
    async def send_text(self, to, body):
        raise WhatsAppUndeliverable(131026, None, "Message Undeliverable.")


@pytest.mark.asyncio
async def test_an_undeliverable_send_marks_the_chat_blocked_once():
    api = FakeApi(turn=turn_result(reply="a\n\nb", vessel_outputs=["OUT1", "OUT2"]))
    await run(text_msg("hello"), api, wa=UnreachableWa())
    assert api.calls.count("blocked") == 1       # not once per chunk


# ---------------------------------------------------------------------------
# review findings, pinned: delivery accounting and the double-send trap
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_swallowed_send_is_not_counted_as_delivered():
    """WindowClosed/undeliverable sends are swallowed by the channel guard —
    they must come back False so Progress never counts them delivered."""
    class ClosedWa(FakeWa):
        async def send_text(self, to, body):
            raise WhatsAppWindowClosed(131047, None, "window closed")

    ch = WhatsAppChannel(ClosedWa(), FakeApi())
    assert await ch.send(NUM, "hello") is False


@pytest.mark.asyncio
async def test_a_first_chunk_failure_is_not_sent_twice():
    """The placeholder-edit fallback is Telegram-only. On WhatsApp a failed
    first chunk used to be re-sent by that fallback — a duplicate message
    whenever the first attempt had actually reached Meta."""
    attempts = []

    class FlakyWa(FakeWa):
        async def send_text(self, to, body):
            attempts.append(body)
            raise WhatsAppError(400, None, "boom")

    api = FakeApi(turn=turn_result(reply="one-chunk answer"))
    await run(text_msg("q"), api, wa=FlakyWa())
    assert attempts.count(attempts[0]) == 1  # one attempt per chunk, no retry-send


# ---------------------------------------------------------------------------
# /short — the Short Description Generator's own command
# ---------------------------------------------------------------------------
TOOLS = [
    {"id": "tool:shortdesc:geared", "name": "Short desc · Geared", "kind": "tool"},
    {"id": "tool:shortdesc:pmx", "name": "Short desc · PMX", "kind": "tool"},
]


class ToolsApi(FakeApi):
    def __init__(self, tools=None, **kw):
        super().__init__(**kw)
        self._tools = TOOLS if tools is None else tools

    async def agents(self, chat_id):
        res = await super().agents(chat_id)
        return {**res, "tools": self._tools}


@pytest.mark.asyncio
async def test_short_opens_the_format_picker_without_agent_extras():
    """/short is a mode chooser: only the formats — no user agents, no
    'Full text' row. The sentinel ids carry colons — what ref() exists for."""
    api = ToolsApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    wa = await run(text_msg("short"), api)
    _, _, rows = wa.lists[0]
    titles = [r["title"] for r in rows]
    assert titles == ["Short desc · Geared", "Short desc · PMX"]
    assert all("Full text" not in t and "Freight" not in t for t in titles)

    row = next(r for r in rows if r["title"] == "Short desc · Geared")
    await run(interactive("list_reply", row["id"]), api)
    assert api.selected == "tool:shortdesc:geared"


@pytest.mark.asyncio
async def test_agents_no_longer_lists_the_generator():
    api = ToolsApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    wa = await run(text_msg("agents"), api)
    _, _, rows = wa.lists[0]
    assert all("Short desc" not in r["title"] for r in rows)


@pytest.mark.asyncio
async def test_short_with_a_format_arg_selects_directly():
    api = ToolsApi()
    wa = await run(text_msg("/short pmx"), api)
    assert api.selected == "tool:shortdesc:pmx"
    assert "paste a full vessel description" in wa.all_text.lower()


@pytest.mark.asyncio
async def test_short_against_an_old_platform_says_so():
    """No `tools` key (backend not updated yet) → a plain message, never an
    empty menu."""
    wa = await run(text_msg("short"), FakeApi())
    assert S.TOOLS_UNAVAILABLE in wa.all_text


@pytest.mark.asyncio
async def test_a_short_description_turn_arrives_verbatim_and_unfenced():
    """The backend returns the generated line as vessel_outputs — the one
    channel-side guarantee that matters is byte-for-byte delivery."""
    rendered = "MV TEST dwt/24(yard)\nDWT 63,000 on 13.2m\nTPC 55/GR 77,000 CBM"
    api = FakeApi(turn=turn_result(vessel_outputs=[rendered]))
    wa = await run(text_msg("Supramax 63k built 2024 ..."), api)
    assert wa.texts[0][1] == rendered
