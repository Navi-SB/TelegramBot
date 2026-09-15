"""What the bot actually does, with fake Telegram and platform clients.

handle_update is a pure function of (update, tg, api), which is what makes
this testable without a bot token, a network, or Vercel.
"""
import ast
import asyncio
import base64
import json
import logging
import re
from pathlib import Path

import pytest

from bot import strings as S
from bot.core.attachments import MAX_BYTES, AttachmentTooLarge, AttachmentUnavailable
from bot.dispatch import handle_update
from bot.platform.client import PlatformError, PlatformUnavailable
from bot.telegram.api import TelegramError
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
    def __init__(self, *, linked=True, agents=None, turn=None, fail=None, account=None,
                 user=None, active_gone=None):
        self._linked = linked
        # The platform's "whose agents are these" label; None is a platform
        # that predates it, which sends no key at all.
        self._account = account
        self._user = user or {"id": "u1", "name": "Alex", "email": "a@x.com"}
        # "agent", "format" or "workflow" when the chat's choice is gone.
        self._active_gone = active_gone
        self._agents = agents or []
        self._turn = turn or {"reply": "hello", "vessel_outputs": [], "pending": [],
                              "tools_used": [], "stop_reason": "end_turn", "error": None}
        self._fail = fail
        self.calls: list[str] = []
        self.revoked = False
        self.selected = None
        self.turns: list[tuple[str, dict | None]] = []  # (content, attachment)

    async def get_link(self, chat_id):
        self.calls.append("get_link")
        if self._fail == "down":
            raise PlatformUnavailable(0, "ConnectError")
        if not self._linked:
            return {"linked": False}
        link = {"linked": True, "user": self._user}
        return {**link, "account": self._account} if self._account else link

    async def redeem(self, chat_id, code, tg_user_id, name):
        self.calls.append("redeem")
        if code == "bad":
            raise PlatformError(404, "invalid_or_expired_code")
        res = {"user": self._user}
        return {**res, "account": self._account} if self._account else res

    async def agents(self, chat_id):
        self.calls.append("agents")
        res = {"agents": self._agents, "active_preset_id": None}
        if self._active_gone:
            res["active_gone"] = self._active_gone
        return {**res, "account": self._account} if self._account else res

    async def set_session(self, chat_id, preset_id=None, *, new_thread=False):
        self.calls.append("set_session")
        self.selected = preset_id
        name = next((a["name"] for a in self._agents if a["id"] == preset_id), None)
        return {"preset_id": preset_id, "preset_name": name, "thread_id": "t1",
                "turns": 3, "rotated": new_thread}

    async def turn(self, chat_id, content, *, attachment=None, timeout):
        self.calls.append("turn")
        self.turns.append((content, attachment))
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
    api = FakeApi()
    tg = await run(u, api)
    assert S.TEXT_ONLY in tg.all_text
    assert "PDF" in S.TEXT_ONLY and ".docx" in S.TEXT_ONLY  # says what DOES work
    assert api.calls == []  # refused before any network call


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


SEAT = {"id": "u9", "name": "Olivia Ops", "email": "ops1@seat.invalid"}


@pytest.mark.asyncio
async def test_pairing_names_a_company_login_by_company_not_its_placeholder_email():
    """A company login's email is a made-up address nobody has seen; the live
    test showed "Connected to Olivia Ops (ops1@seat.invalid)"."""
    tg = await run(msg("/start code"), FakeApi(linked=False, user=SEAT,
                                                account="Acme <Shipping> (ops1)"))
    assert "seat.invalid" not in tg.all_text
    assert tg.sent[0][1] == S.linked("Olivia Ops", "Acme <Shipping> (ops1)")
    assert tg.sent[0][1].startswith(
        "🔗 Connected to <b>Olivia Ops</b> — Acme &lt;Shipping&gt; (ops1).\n")

    # A personal account is still named by its email.
    tg = await run(msg("/start code"), FakeApi(linked=False, account="a@x.com"))
    assert tg.sent[0][1].startswith("🔗 Connected to <b>Alex</b> — a@x.com.\n")

    # A platform that sends no label: the placeholder is still never shown.
    tg = await run(msg("/start code"), FakeApi(linked=False, user=SEAT))
    assert tg.sent[0][1].startswith("🔗 Connected to <b>Olivia Ops</b>.\n")
    tg = await run(msg("/start code"), FakeApi(linked=False))
    assert tg.sent[0][1].startswith("🔗 Connected to <b>Alex</b> — a@x.com.\n")


@pytest.mark.asyncio
async def test_status_names_a_company_login_by_company_not_its_placeholder_email():
    """It showed "Account: ops1@seat.invalid" above "Agents from: Acme
    Shipping (ops1)". The label is the account, said once."""
    tg = await run(msg("/status"), FakeApi(user=SEAT, account="Acme Shipping (ops1)"))
    text = tg.sent[-1][1]
    assert "seat.invalid" not in text
    assert text.startswith("<b>Account:</b> Acme Shipping (ops1)\n<b>Agent:</b>")

    tg = await run(msg("/status"), FakeApi(user=SEAT))
    assert tg.sent[-1][1].startswith("<b>Account:</b> unknown\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/new", "/status"])
async def test_new_and_status_say_the_chats_agent_was_deleted(command):
    """The live test: with the agent deleted in the web app, /new said "Fresh
    conversation with plain text" and /status "plain text (no agent)", with no
    reason, while an ordinary message explained. They moved the chat to plain
    text all the same."""
    api = FakeApi(active_gone="agent", account="Acme Shipping (ops1)")
    tg = await run(msg(command), api)
    text = tg.sent[-1][1]
    assert text.startswith(
        "⚠️ The agent this chat was using was deleted or isn't on "
        "<b>Acme Shipping (ops1)</b>, so this chat now answers in plain text.\n"
    )
    assert ("Fresh conversation with plain text" if command == "/new"
            else "plain text (no agent)") in text
    assert api.selected is None

    # A chat simply on plain text gets no note.
    tg = await run(msg(command), FakeApi(account="Acme Shipping (ops1)"))
    assert "⚠️" not in tg.sent[-1][1]


def test_a_gone_workflow_or_format_is_named_as_one():
    assert S.active_gone("workflow", "Acme").startswith(
        "⚠️ The workflow this chat was using is switched off or was deleted, so")
    assert S.active_gone("format").startswith(
        "⚠️ The format this chat was using was deleted or isn't on the account this chat")


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


# ---------------------------------------------------------------------------
# files (NAV-81) — the bridge fetches and forwards; the backend reads
# ---------------------------------------------------------------------------
PDF = b"%PDF-1.7\n" + b"x" * 2048


class FileTg(FakeTg):
    """FakeTg that can serve one file: getFile, then the download."""

    def __init__(self, data=PDF, *, file_size=None, get_file_error=None,
                 download_error=None, download_delay=0.0):
        super().__init__()
        self._data = data
        self._file_size = len(data) if file_size is None else file_size
        self._get_file_error = get_file_error
        self._download_error = download_error
        self._download_delay = download_delay
        self.get_file_calls: list[str] = []
        self.downloads: list[str] = []

    async def get_file(self, file_id):
        self.get_file_calls.append(file_id)
        if self._get_file_error:
            raise self._get_file_error
        return {"file_id": file_id, "file_size": self._file_size,
                "file_path": "documents/file_7.pdf"}

    async def download_file(self, file_path, max_bytes):
        self.downloads.append(file_path)
        if self._download_delay:
            await asyncio.sleep(self._download_delay)
        if self._download_error:
            raise self._download_error
        return self._data


def doc(caption=None, *, name="Q88 MV OCEAN STAR.pdf", mime="application/pdf",
        size=len(PDF), chat=1):
    m = {"chat": {"id": chat, "type": "private"},
         "from": {"id": 77, "first_name": "Alex"},
         "document": {"file_id": "BQACAgQ", "file_unique_id": "u1",
                      "file_name": name, "mime_type": mime, "file_size": size}}
    if caption is not None:
        m["caption"] = caption
    return {"update_id": 9, "message": m}


def logged(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_document_with_a_caption_reaches_the_platform_with_its_bytes():
    api = FakeApi()
    tg = await run(doc("short desc please"), api, FileTg())
    [(content, attachment)] = api.turns
    assert content == "short desc please"  # the caption is the instruction
    assert attachment == {"filename": "Q88 MV OCEAN STAR.pdf",
                          "mime_type": "application/pdf",
                          "data_b64": base64.b64encode(PDF).decode()}
    assert tg.get_file_calls == ["BQACAgQ"]
    assert "hello" in tg.edits[0][1]  # the answer replaced the placeholder


@pytest.mark.asyncio
async def test_a_document_without_a_caption_sends_an_empty_instruction():
    api = FakeApi()
    await run(doc(), api, FileTg())
    [(content, attachment)] = api.turns
    assert content == ""
    assert attachment["data_b64"]


@pytest.mark.asyncio
async def test_a_caption_is_never_read_as_a_command():
    """'/new' typed under a PDF is an instruction about the PDF — starting a
    fresh thread and dropping the file would lose what the user sent."""
    for caption in ("/new", "new", "/help", "/start somecode"):
        api = FakeApi()
        await run(doc(caption), api, FileTg())
        assert api.turns and api.turns[0][0] == caption, caption
        assert "set_session" not in api.calls and "redeem" not in api.calls


@pytest.mark.asyncio
async def test_a_plain_words_switch_under_a_file_goes_with_the_file():
    """"new conversation" or "switch to PMX" (NAV-80) are recognised by the
    platform, and only in a message: under a file the bridge sends them as
    the caption next to the file, the same as any other caption."""
    for caption in ("new conversation", "list my agents", "switch to PMX Short"):
        api = FakeApi()
        await run(doc(caption), api, FileTg())
        [(content, attachment)] = api.turns
        assert content == caption
        assert attachment["data_b64"] == base64.b64encode(PDF).decode()
        assert "set_session" not in api.calls


@pytest.mark.asyncio
async def test_the_placeholder_says_the_file_is_being_read():
    tg = await run(doc(), FakeApi(), FileTg())
    assert tg.sent[0][1] == S.READING_FILE


@pytest.mark.asyncio
async def test_an_unlinked_chat_never_downloads_a_file():
    api = FakeApi(linked=False)
    tg = await run(doc("describe this"), api, FileTg())
    assert S.NOT_LINKED in tg.all_text
    assert tg.get_file_calls == [] and tg.downloads == []
    assert "turn" not in api.calls


@pytest.mark.asyncio
async def test_a_declared_oversize_file_is_refused_without_downloading():
    api = FakeApi()
    tg = await run(doc(size=MAX_BYTES + 1), api, FileTg())
    assert tg.all_text.strip() == S.FILE_TOO_LARGE  # no placeholder either
    assert tg.get_file_calls == [] and tg.downloads == []
    assert "turn" not in api.calls


@pytest.mark.asyncio
async def test_a_file_that_turns_out_bigger_than_declared_is_refused():
    """Declared sizes are hints; the stream enforces the cap and the user
    hears the same size message, never a generic error."""
    api = FakeApi()
    tg = await run(doc(size=None), api, FileTg(download_error=AttachmentTooLarge()))
    assert S.FILE_TOO_LARGE in tg.edits[0][1]
    assert "turn" not in api.calls


@pytest.mark.asyncio
async def test_telegram_refusing_a_huge_file_is_a_size_message_not_a_retry():
    """Past the cloud Bot API's 20 MB, getFile itself fails — "try again"
    would never work, so it must read as too large."""
    tg = FileTg(get_file_error=TelegramError(400, "Bad Request: file is too big"))
    api = FakeApi()
    await run(doc(size=None), api, tg)
    assert S.FILE_TOO_LARGE in tg.edits[0][1]
    assert tg.downloads == [] and "turn" not in api.calls


@pytest.mark.asyncio
async def test_a_failed_download_says_the_file_was_not_processed():
    for tg in (FileTg(download_error=AttachmentUnavailable("download_404")),
               FileTg(get_file_error=TelegramError(400, "Bad Request: wrong file_id"))):
        api = FakeApi()
        await run(doc(), api, tg)
        assert S.FILE_UNAVAILABLE in tg.edits[0][1]
        assert "turn" not in api.calls


@pytest.mark.asyncio
async def test_a_download_that_hangs_gives_up_inside_the_deadline():
    api = FakeApi()
    tg = FileTg(download_delay=5)
    await handle_update(doc(), tg, api, turn_timeout=0.1)
    assert S.FILE_UNAVAILABLE in tg.edits[0][1]
    assert "turn" not in api.calls


@pytest.mark.asyncio
async def test_the_download_time_comes_off_the_turn_deadline(monkeypatch):
    import bot.core.dispatch as core

    ticks = iter([100.0, 112.0])

    class Clock:  # only dispatch's clock — asyncio keeps the real one
        monotonic = staticmethod(lambda: next(ticks))

    monkeypatch.setattr(core, "time", Clock)
    seen = []

    class Api(FakeApi):
        async def turn(self, chat_id, content, *, attachment=None, timeout):
            seen.append(timeout)
            return await super().turn(chat_id, content, attachment=attachment,
                                      timeout=timeout)

    await handle_update(doc(), FileTg(), Api(), turn_timeout=30)
    assert seen == [18.0]  # 12 s went on the download


@pytest.mark.asyncio
async def test_an_old_platform_rejecting_the_file_says_files_are_not_supported_yet():
    """Backend deploys first, but in the window before it does, a 422 on a
    file turn is 'not yet' — not 'something went wrong, try again'."""
    class OldApi(FakeApi):
        async def turn(self, chat_id, content, *, attachment=None, timeout):
            raise PlatformError(422, [{"type": "string_too_short", "loc": ["body", "content"]}])

    tg = await run(doc(), OldApi(), FileTg())
    assert S.FILES_NOT_SUPPORTED_YET in tg.edits[0][1]

    tg = await run(msg("hello"), OldApi())  # a text turn keeps its old meaning
    assert S.UNEXPECTED in tg.edits[0][1]
    assert S.FILES_NOT_SUPPORTED_YET not in tg.all_text


@pytest.mark.asyncio
async def test_a_proxy_refusing_the_upload_is_not_blamed_on_the_files_size(caplog):
    """Nothing over the cap is ever sent, so a 413 is a body limit in the
    proxy in front of the platform, not the file."""
    caplog.set_level(logging.INFO, logger="tropis.bot")

    class ProxiedApi(FakeApi):
        async def turn(self, chat_id, content, *, attachment=None, timeout):
            raise PlatformError(413, "Request Entity Too Large")

    tg = await run(doc(), ProxiedApi(), FileTg())
    assert S.FILE_NOT_DELIVERED in tg.edits[0][1]
    assert S.FILE_TOO_LARGE not in tg.all_text
    assert "upload_413" in logged(caplog)


@pytest.mark.asyncio
async def test_files_sent_faster_than_the_platform_takes_them_are_told_to_wait(caplog):
    """VoyageCalc takes 6 messages a minute per chat and answers the 7th with a
    429. Selecting 7 or more files sends them together, so the rest got
    "Something went wrong on my side. Try again, or /new." — but nothing broke
    and /new does not help: waiting does."""
    caplog.set_level(logging.INFO, logger="tropis.bot")

    class BusyApi(FakeApi):
        async def turn(self, chat_id, content, *, attachment=None, timeout):
            raise PlatformError(429, "too_many_attempts", retry_after=42)

    tg = await run(doc(), BusyApi(), FileTg())
    assert tg.edits[0][1] == S.rate_limited("a minute", daily=False, file=True)
    assert "that file wasn't read" in tg.edits[0][1] and "in a minute" in tg.edits[0][1]
    assert S.UNEXPECTED not in tg.all_text
    assert "turn_rate_limited" in logged(caplog)

    tg = await run(msg("hello"), BusyApi())
    assert tg.edits[0][1] == S.rate_limited("a minute", daily=False, file=False)
    assert "that message wasn't processed" in tg.edits[0][1]


@pytest.mark.asyncio
async def test_the_daily_limit_says_when_to_send_again():
    """The per-account daily ceiling is a 429 too, and its Retry-After can be
    hours: "in a minute" would be untrue there."""
    class SpentApi(FakeApi):
        async def turn(self, chat_id, content, *, attachment=None, timeout):
            raise PlatformError(429, "too_many_attempts", retry_after=3 * 3600 - 100)

    tg = await run(msg("hello"), SpentApi())
    assert tg.edits[0][1] == S.rate_limited("about 3 hours", daily=True, file=False)
    assert "daily" in tg.edits[0][1]
    assert S.UNEXPECTED not in tg.all_text


@pytest.mark.parametrize("seconds, words", [
    (None, "a minute"), (1, "a minute"), (61, "a minute"), (120, "a minute"),
    (121, "about 3 minutes"), (40 * 60, "about 40 minutes"), (3600, "about 1 hour"),
    (86401, "about 24 hours"),
])
def test_how_long_to_wait_is_said_in_words(seconds, words):
    from bot.core.dispatch import _wait_words

    assert _wait_words(seconds) == words


@pytest.mark.asyncio
async def test_the_platform_client_keeps_retry_after_from_a_429():
    import httpx

    from bot.platform.client import PlatformClient

    def handler(request):
        return httpx.Response(429, json={"detail": "too_many_attempts"}, headers={"Retry-After": "37"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        api = PlatformClient("http://platform.test", "tok", http)
        with pytest.raises(PlatformError) as caught:
            await api.turn("1", "hello", timeout=5)
    assert caught.value.status == 429
    assert caught.value.detail == "too_many_attempts"
    assert caught.value.retry_after == 37


@pytest.mark.asyncio
async def test_a_chat_the_platform_no_longer_accepts_gets_the_pairing_message():
    """Unlinked or suspended after the link check: say what that check says,
    not "something went wrong"."""
    class RefusingApi(FakeApi):
        async def turn(self, chat_id, content, *, attachment=None, timeout):
            raise PlatformError(403, "chat_not_linked")

    for update, tg in ((doc(), FileTg()), (msg("hello"), FakeTg())):
        tg = await run(update, RefusingApi(), tg)
        assert S.NOT_LINKED in tg.edits[0][1]
        assert S.UNEXPECTED not in tg.all_text


@pytest.mark.asyncio
async def test_a_file_the_platform_cannot_read_is_answered_like_any_reply():
    reason = "That's an old Word .doc — save it as .docx or PDF and send it again."
    api = FakeApi(turn={"reply": reason, "vessel_outputs": [], "pending": [],
                        "tools_used": [], "stop_reason": "attachment_rejected",
                        "error": None})
    tg = await run(doc(name="recap.doc", mime="application/msword"), api, FileTg())
    assert "save it as .docx" in tg.edits[0][1]
    assert S.NO_ANSWER not in tg.all_text


@pytest.mark.asyncio
async def test_file_names_and_captions_never_reach_the_logs(caplog):
    caplog.set_level(logging.INFO, logger="tropis.bot")
    await run(doc("recap for MV SECRET CHARTERER"), FakeApi(), FileTg())
    await run(doc(size=MAX_BYTES + 1), FakeApi(), FileTg())
    await run(doc(), FakeApi(), FileTg(download_error=AttachmentUnavailable("download_500")))
    text = logged(caplog)
    assert "OCEAN STAR" not in text and "SECRET" not in text and "file_7" not in text
    events = [json.loads(r.getMessage()) for r in caplog.records]
    assert next(e for e in events if e["event"] == "turn_done")["kind"] == "file"
    assert any(e.get("outcome") == "declared_too_large" for e in events)
    assert any(e.get("outcome") == "download_500" for e in events)
    assert not any(k.endswith("__dropped") for e in events for k in e)


@pytest.mark.asyncio
async def test_refused_media_is_logged_by_kind_without_its_caption(caplog):
    """Until now a refused photo left no trace, so nobody could tell how often
    people try. The kind and declared type are enough to measure that."""
    caplog.set_level(logging.INFO, logger="tropis.bot")
    u = msg("")
    u["message"].pop("text")
    u["message"]["caption"] = "MV SECRET position list"
    u["message"]["voice"] = {"file_id": "v1", "mime_type": "audio/ogg", "duration": 3}
    tg = await run(u, FakeApi())
    assert S.TEXT_ONLY in tg.all_text
    [event] = [json.loads(r.getMessage()) for r in caplog.records]
    assert event["event"] == "media_refused"
    assert (event["kind"], event["mime"]) == ("voice", "audio/ogg")
    assert "SECRET" not in logged(caplog)


@pytest.mark.asyncio
async def test_a_gif_is_refused_even_though_telegram_also_calls_it_a_document():
    u = msg("")
    u["message"].pop("text")
    u["message"]["animation"] = {"file_id": "g1", "mime_type": "video/mp4"}
    u["message"]["document"] = {"file_id": "g1", "mime_type": "video/mp4"}
    tg = FileTg()
    api = FakeApi()
    await run(u, api, tg)
    assert S.TEXT_ONLY in tg.all_text
    assert tg.get_file_calls == [] and api.calls == []


def test_the_size_message_states_the_real_cap():
    assert f"{MAX_BYTES // (1024 * 1024)} MB" in S.FILE_TOO_LARGE


def test_the_cap_is_the_number_voyagecalc_allows():
    """VoyageCalc's attachments.MAX_ATTACHMENT_BYTES is pinned to the same 5 MiB
    by its own test, and is its web page's cap too. At 5,000,000 here a file
    that uploaded on the page was refused by both bots, all saying "5 MB"."""
    assert MAX_BYTES == 5 * 1024 * 1024


def test_help_says_files_can_be_sent_with_a_caption():
    assert "PDF or Word file" in S.HELP and "caption" in S.HELP
