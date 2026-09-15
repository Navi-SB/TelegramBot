"""What the bot actually does, with fake Telegram and platform clients.

handle_update is a pure function of (update, tg, api), which is what makes
this testable without a bot token, a network, or Vercel.
"""
import ast
import re
from pathlib import Path

import pytest

from bot import strings as S
from bot.dispatch import handle_update
from bot.platform.client import PlatformError, PlatformUnavailable
from bot.telegram.keyboards import encode, ref


class FakeTg:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.edits: list[tuple[int, str]] = []
        self.keyboards: list[dict] = []
        self.answered: list[str] = []
        self._next_id = 100

    async def send_safe(self, chat_id, text, *, reply_markup=None):
        self.sent.append((str(chat_id), text))
        if reply_markup:
            self.keyboards.append(reply_markup)
        self._next_id += 1
        return self._next_id

    async def send_message(self, chat_id, text, **kw):
        return await self.send_safe(chat_id, text)

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edits.append((message_id, text))

    async def edit_reply_markup(self, *a, **kw):
        pass

    async def answer_callback(self, cid, text=None, *, alert=False):
        self.answered.append(cid)

    async def send_chat_action(self, *a, **kw):
        pass

    @property
    def all_text(self) -> str:
        return "\n".join(t for _, t in self.sent) + "\n".join(t for _, t in self.edits)


class FakeApi:
    def __init__(self, *, linked=True, agents=None, turn=None, fail=None, account=None):
        self._linked = linked
        # The platform's "whose agents are these" label; None is a platform
        # that predates it, which sends no key at all.
        self._account = account
        self._agents = agents or []
        self._turn = turn or {"reply": "hello", "vessel_outputs": [], "pending": [],
                              "tools_used": [], "stop_reason": "end_turn", "error": None}
        self._fail = fail
        self.calls: list[str] = []
        self.revoked = False
        self.selected = None

    async def get_link(self, chat_id):
        self.calls.append("get_link")
        if self._fail == "down":
            raise PlatformUnavailable(0, "ConnectError")
        if not self._linked:
            return {"linked": False}
        link = {"linked": True, "user": {"id": "u1", "name": "Alex", "email": "a@x.com"}}
        return {**link, "account": self._account} if self._account else link

    async def redeem(self, chat_id, code, tg_user_id, name):
        self.calls.append("redeem")
        if code == "bad":
            raise PlatformError(404, "invalid_or_expired_code")
        return {"user": {"id": "u1", "name": "Alex", "email": "a@x.com"}}

    async def agents(self, chat_id):
        self.calls.append("agents")
        res = {"agents": self._agents, "active_preset_id": None}
        return {**res, "account": self._account} if self._account else res

    async def set_session(self, chat_id, preset_id=None, *, new_thread=False):
        self.calls.append("set_session")
        self.selected = preset_id
        name = next((a["name"] for a in self._agents if a["id"] == preset_id), None)
        return {"preset_id": preset_id, "preset_name": name, "thread_id": "t1",
                "turns": 3, "rotated": new_thread}

    async def turn(self, chat_id, content, *, timeout):
        self.calls.append("turn")
        if self._fail == "turn_down":
            raise PlatformUnavailable(0, "ReadTimeout")
        return self._turn

    async def confirm(self, chat_id, pending_id, action):
        self.calls.append(f"confirm:{action}")
        if self._fail == "already":
            raise PlatformError(409, "already applied")
        return {"status": "applied"}

    async def revoke(self, chat_id):
        self.revoked = True

    async def mark_blocked(self, chat_id):
        self.calls.append("blocked")


def msg(text, chat=1, chat_type="private"):
    return {"update_id": 1, "message": {"chat": {"id": chat, "type": chat_type},
                                        "from": {"id": 77, "first_name": "Alex"}, "text": text}}


def cb(data, chat=1):
    return {"update_id": 2, "callback_query": {"id": "cb1", "data": data,
            "from": {"id": 77, "first_name": "Alex"},
            "message": {"chat": {"id": chat, "type": "private"}}}}


async def run(update, api, tg=None):
    tg = tg or FakeTg()
    await handle_update(update, tg, api, turn_timeout=30)
    return tg


# ---------------------------------------------------------------------------
# unlinked
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_unlinked_chat_only_ever_gets_pairing_instructions():
    api = FakeApi(linked=False)
    tg = await run(msg("what vessels do I have?"), api)
    assert S.NOT_LINKED in tg.all_text
    # No account was created, no turn was run.
    assert "turn" not in api.calls


@pytest.mark.asyncio
async def test_the_unlinked_message_reveals_nothing():
    """Telegram bots are publicly discoverable, so this string is
    internet-facing. It must not hint whether an account exists."""
    tg = await run(msg("hi"), FakeApi(linked=False))
    text = tg.all_text.lower()
    for leak in ("unknown user", "not found", "no account with", "77"):
        assert leak not in text


@pytest.mark.asyncio
async def test_start_with_a_code_is_the_one_unlinked_action():
    api = FakeApi(linked=False)
    tg = await run(msg("/start somecode"), api)
    assert "redeem" in api.calls
    assert "Connected to" in tg.all_text


@pytest.mark.asyncio
async def test_a_bad_code_says_so_without_detail():
    tg = await run(msg("/start bad"), FakeApi(linked=False))
    assert S.LINK_FAILED in tg.all_text


# ---------------------------------------------------------------------------
# guard rails
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_group_chats_are_refused():
    """chat_id is the group but from.id is whoever spoke, so a chat-level link
    would let any member drive a write-capable agent against the linker's data."""
    api = FakeApi()
    tg = await run(msg("hello", chat_type="supergroup"), api)
    assert S.GROUPS_UNSUPPORTED in tg.all_text
    assert api.calls == []  # refused before any network call


@pytest.mark.asyncio
async def test_help_needs_no_network():
    api = FakeApi()
    tg = await run(msg("/help"), api)
    assert "Tropis assistant" in tg.all_text
    assert api.calls == []


def test_help_says_how_to_switch_agents_by_name():
    assert "/agent &lt;name&gt;" in S.HELP
    assert "switch to &lt;name&gt;" in S.HELP


def test_every_command_help_lists_is_in_the_telegram_command_menu():
    """The menu Telegram shows when you type '/' is registered by
    scripts/set_webhook.py; a command missing there is one most users never
    find. Read as source because running that module talks to Telegram."""
    tree = ast.parse((Path(__file__).parents[1] / "scripts" / "set_webhook.py").read_text())
    commands = next(ast.literal_eval(node.value) for node in tree.body
                    if isinstance(node, ast.Assign) and node.targets[0].id == "COMMANDS")
    registered = {c["command"] for c in commands}
    in_help = set(re.findall(r"^/(\w+)", S.HELP, re.M))
    assert "agent" in in_help
    assert in_help <= registered


@pytest.mark.asyncio
async def test_a_photo_gets_a_useful_refusal():
    u = msg("")
    u["message"].pop("text")
    u["message"]["photo"] = [{"file_id": "x"}]
    tg = await run(u, FakeApi())
    assert S.TEXT_ONLY in tg.all_text


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_agent_picker_offers_every_agent_plus_plain_text():
    api = FakeApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    tg = await run(msg("/agents"), api)
    buttons = [b["text"] for row in tg.keyboards[0]["inline_keyboard"] for b in row]
    assert "Freight Desk" in buttons
    assert any("Full text" in b for b in buttons)


@pytest.mark.asyncio
async def test_tapping_an_agent_selects_it_by_ref():
    api = FakeApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    tg = await run(cb(encode("a", ref("a1"))), api)
    assert api.selected == "a1"
    assert "Freight Desk" in tg.all_text


@pytest.mark.asyncio
async def test_a_stale_keyboard_says_so_instead_of_doing_nothing():
    tg = await run(cb("0:a:oldref"), FakeApi())
    assert S.STALE_MENU in tg.all_text


@pytest.mark.asyncio
async def test_agent_by_name_is_case_insensitive():
    api = FakeApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    await run(msg("/agent freight desk"), api)
    assert api.selected == "a1"


@pytest.mark.asyncio
async def test_every_confirmation_says_how_to_switch_later():
    """The menu only shows up by itself right after linking; each of these is
    the moment a user learns there is a way back to it."""
    api = FakeApi(linked=False, agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    tg = await run(msg("/start somecode"), api)
    assert "/agents" in tg.sent[0][1]                   # the "Connected" message itself

    api = FakeApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    for command in ("/agent freight desk", "/new"):
        tg = await run(msg(command), api)
        assert S.SWITCH_HINT in tg.sent[-1][1], command

    tg = await run(cb(encode("a", "-")), api)
    assert S.SWITCH_HINT in tg.sent[-1][1]              # Full text is a choice too


@pytest.mark.asyncio
async def test_the_agent_menu_says_whose_agents_it_is_showing():
    """A chat linked to a personal account can't offer an agent that lives in
    the company's shared agents. Naming the account makes that visible."""
    api = FakeApi(agents=[{"id": "a1", "name": "Default", "kind": "template"}],
                  account="Acme <Shipping> & Co")
    tg = await run(msg("/agents"), api)
    assert tg.sent[-1][1] == S.pick_agent("Acme <Shipping> & Co")
    assert "Showing the agents for Acme &lt;Shipping&gt; &amp; Co." in tg.sent[-1][1]


@pytest.mark.asyncio
async def test_the_agent_menu_goes_without_an_account_a_platform_doesnt_send():
    api = FakeApi(agents=[{"id": "a1", "name": "Default", "kind": "template"}])
    tg = await run(msg("/agents"), api)
    assert tg.sent[-1][1] == S.PICK_AGENT


@pytest.mark.asyncio
async def test_status_names_whose_agents_the_chat_uses():
    tg = await run(msg("/status"), FakeApi(account="Acme Shipping"))
    assert "<b>Agents from:</b> Acme Shipping" in tg.sent[-1][1]
    assert "<b>Account:</b> a@x.com" in tg.sent[-1][1]

    tg = await run(msg("/status"), FakeApi())
    assert "Agents from" not in tg.sent[-1][1]
    assert "<b>Account:</b> a@x.com" in tg.sent[-1][1]

    # A personal account labelled by its email would only say it twice.
    tg = await run(msg("/status"), FakeApi(account="a@x.com"))
    assert "Agents from" not in tg.sent[-1][1]


@pytest.mark.asyncio
async def test_status_takes_the_account_from_the_agent_list_when_the_link_has_none():
    class Api(FakeApi):
        async def get_link(self, chat_id):
            link = await super().get_link(chat_id)
            return {k: v for k, v in link.items() if k != "account"}

    tg = await run(msg("/status"), Api(account="Acme Shipping"))
    assert "<b>Agents from:</b> Acme Shipping" in tg.sent[-1][1]


@pytest.mark.asyncio
async def test_no_agent_match_names_the_account_it_searched():
    api = FakeApi(agents=[{"id": "a1", "name": "Default", "kind": "template"}],
                  account="alex@personal.com")
    tg = await run(msg("/agent PMX Short"), api)
    assert tg.sent[-1][1] == (
        "No agent matches <b>PMX Short</b> in the agents for "
        "<b>alex@personal.com</b>. Try /agents."
    )
    assert api.selected is None


@pytest.mark.asyncio
async def test_no_agents_points_at_agent_studio():
    tg = await run(msg("/agents"), FakeApi(agents=[]))
    assert "Agent Studio" in tg.all_text


# ---------------------------------------------------------------------------
# the turn
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_turn_replaces_the_placeholder_with_the_answer():
    api = FakeApi(turn={"reply": "You have 12 Panamaxes.", "vessel_outputs": [],
                        "pending": [], "tools_used": ["search_vessels"],
                        "stop_reason": "end_turn", "error": None})
    tg = await run(msg("how many panamaxes?"), api)
    assert S.THINKING in tg.sent[0][1]
    assert "12 Panamaxes" in tg.edits[0][1]


@pytest.mark.asyncio
async def test_vessel_outputs_go_first_and_verbatim():
    """These are the user's own configured templates — what gets pasted into
    a broker's WhatsApp. Never re-wrapped or 'improved'."""
    rendered = "MV OCEAN STAR - 82,000 MT DWT ON 14.5 M SSW"
    api = FakeApi(turn={"reply": "Here you go.", "vessel_outputs": [rendered],
                        "pending": [], "tools_used": [], "stop_reason": "end_turn",
                        "error": None})
    tg = await run(msg("describe ocean star"), api)
    assert rendered in tg.edits[0][1]


@pytest.mark.asyncio
async def test_a_write_proposal_becomes_approve_reject_buttons():
    api = FakeApi(turn={"reply": "", "vessel_outputs": [], "tools_used": [],
                        "stop_reason": "needs_confirmation", "error": None,
                        "pending": [{"pending_id": "p1", "tool": "add_fixtures",
                                     "diff": {"summary": "Add 2 fixtures"}}]})
    tg = await run(msg("log these fixtures"), api)
    buttons = [b["text"] for row in tg.keyboards[-1]["inline_keyboard"] for b in row]
    assert "✅ Approve" in buttons and "❌ Reject" in buttons


@pytest.mark.asyncio
async def test_a_double_tapped_approve_reads_409_as_success():
    """The DB is the real idempotency guard; the bot must not show an error
    for a button someone pressed twice."""
    api = FakeApi(fail="already")
    tg = await run(cb(encode("w", "y", "p1")), api)
    assert "Already resolved" in tg.all_text


@pytest.mark.asyncio
async def test_platform_down_says_the_message_was_not_processed():
    """Explicitly NOT queued — so resending is the correct user response."""
    tg = await run(msg("hello"), FakeApi(fail="down"))
    assert "wasn't processed" in tg.all_text


@pytest.mark.asyncio
async def test_an_agent_error_shows_only_the_exception_class():
    api = FakeApi(turn={"reply": "", "vessel_outputs": [], "pending": [],
                        "tools_used": [], "stop_reason": "error",
                        "error": "ValueError: /opt/voyagecalc/secret/path leaked"})
    tg = await run(msg("hi"), api)
    text = tg.all_text
    assert "ValueError" in text
    assert "/opt/voyagecalc" not in text  # paths, SQL and keys stay out


@pytest.mark.asyncio
async def test_outputs_rendered_before_a_late_error_still_ship():
    """A turn can die AFTER earlier iterations produced rendered outputs
    (bunker prices fetched, then the pool-calc iteration fails). Those are
    answers the user asked for — they go out first, then the error."""
    card = "Singapore bunker prices — 2026-08-04\nVLSFO: 844.5"
    api = FakeApi(turn={"reply": "", "vessel_outputs": [card], "pending": [],
                        "tools_used": [], "stop_reason": "error",
                        "error": "APIError: overloaded"})
    tg = await run(msg("concordia with current prices"), api)
    assert "Singapore bunker prices" in tg.edits[0][1]  # card replaces placeholder
    assert "APIError" in tg.sent[-1][1]  # the error follows as its own message


@pytest.mark.asyncio
async def test_a_turn_that_asks_for_the_agent_menu_shows_it_under_the_reply():
    """'list my agents' in plain words: the platform answers and asks for the
    tappable menu, so the user never has to learn /agents to use it."""
    api = FakeApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}],
                  turn={"reply": "Your agents: Freight Desk.", "vessel_outputs": [],
                        "pending": [], "tools_used": [], "stop_reason": "agent_menu",
                        "error": None, "menu": "agents"})
    tg = await run(msg("list my agents"), api)
    assert "Your agents: Freight Desk." in tg.edits[0][1]  # the reply first
    assert tg.sent[-1][1] == S.PICK_AGENT                   # then the menu
    buttons = [b["text"] for row in tg.keyboards[-1]["inline_keyboard"] for b in row]
    assert "Freight Desk" in buttons
    assert api.calls.index("turn") < api.calls.index("agents")


@pytest.mark.asyncio
async def test_a_turn_without_a_menu_request_shows_no_menu():
    api = FakeApi(agents=[{"id": "a1", "name": "Freight Desk", "kind": "template"}])
    tg = await run(msg("how many panamaxes?"), api)
    assert tg.keyboards == []
    assert "agents" not in api.calls


@pytest.mark.asyncio
async def test_a_menu_that_fails_to_load_does_not_disown_the_delivered_reply():
    """The reply already landed; 'your message wasn't processed' would be a
    lie that invites a resend."""
    class Api(FakeApi):
        async def agents(self, chat_id):
            raise PlatformUnavailable(0, "ReadTimeout")

    api = Api(turn={"reply": "Your agents: Freight Desk.", "vessel_outputs": [],
                    "pending": [], "tools_used": [], "stop_reason": "agent_menu",
                    "error": None, "menu": "agents"})
    tg = await run(msg("list my agents"), api)
    assert "Your agents: Freight Desk." in tg.edits[0][1]
    assert S.PLATFORM_DOWN not in tg.all_text
    assert S.UNEXPECTED not in tg.all_text


# ---------------------------------------------------------------------------
# unlink
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unlink_is_two_step():
    api = FakeApi()
    tg = await run(msg("/unlink"), api)
    assert not api.revoked  # asking is not doing
    assert tg.keyboards

    await run(cb(encode("ul", "y")), api)
    assert api.revoked


@pytest.mark.asyncio
async def test_unlink_can_be_cancelled():
    api = FakeApi()
    tg = await run(cb(encode("ul", "n")), api)
    assert not api.revoked
    assert S.UNLINK_CANCELLED in tg.all_text
