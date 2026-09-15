"""What the WhatsApp bot actually does — the core flows driven through the
WhatsApp channel adapter, with a fake Cloud API client and the same fake
platform the Telegram tests use. The behavioural invariants (reveal-nothing
gate, outputs-before-error, 409 semantics) must hold identically here."""
import base64
import hashlib
import json
import logging

import httpx
import pytest

from bot.core.codec import encode, ref
from bot.core.dispatch import handle_inbound
from bot.platform.client import PlatformError, PlatformUnavailable
from bot.whatsapp import strings as S
from bot.core.attachments import MAX_BYTES
from bot.whatsapp.api import (
    WhatsAppClient, WhatsAppError, WhatsAppUndeliverable, WhatsAppWindowClosed,
)
from bot.whatsapp.channel import WhatsAppChannel
from bot.whatsapp.parse import parse_envelopes
from tests.test_dispatch import FakeApi
from tests.test_wa_parse import NUM, doc_msg, text_msg, wa_body


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


@pytest.mark.asyncio
async def test_media_gets_a_useful_refusal(caplog):
    caplog.set_level(logging.INFO, logger="tropis.bot")
    msg = {"from": NUM, "id": "wamid.M1", "type": "image",
           "image": {"id": "m1", "mime_type": "image/jpeg", "caption": "MV SECRET"}}
    api = FakeApi()
    wa = await run(msg, api)
    assert S.TEXT_ONLY in wa.all_text
    assert api.calls == []
    [event] = [json.loads(r.getMessage()) for r in caplog.records]
    assert (event["event"], event["kind"], event["mime"]) == ("media_refused", "image", "image/jpeg")
    assert event["chat_id"] != NUM  # the keyed log ref, never the phone number
    assert "SECRET" not in caplog.text


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


# ---------------------------------------------------------------------------
# files (NAV-81) — media id → fresh URL → Bearer download → the platform
# ---------------------------------------------------------------------------
DOCX = b"PK\x03\x04" + b"word/document.xml" * 64
MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1234567890"


class FileWa(FakeWa):
    """FakeWa that can serve one media file."""

    def __init__(self, data=DOCX, *, file_size=None, sha256="hex",
                 media_error=None):
        super().__init__()
        self._data = data
        self._file_size = str(len(data)) if file_size is None else file_size
        if sha256 == "hex":
            sha256 = hashlib.sha256(data).hexdigest()
        self._sha256 = sha256
        self._media_error = media_error
        self.media_calls: list[str] = []
        self.downloads: list[str] = []

    async def get_media(self, media_id):
        self.media_calls.append(media_id)
        if self._media_error:
            raise self._media_error
        return {"url": f"{MEDIA_URL}&n={len(self.media_calls)}", "id": media_id,
                "mime_type": "application/pdf", "file_size": self._file_size,
                "sha256": self._sha256}

    async def download_media(self, url, max_bytes):
        self.downloads.append(url)
        return self._data


def wa_doc(caption=None, **over):
    return doc_msg(caption, sha256=hashlib.sha256(DOCX).hexdigest(), **over)


@pytest.mark.asyncio
async def test_a_document_reaches_the_platform_with_its_caption_and_bytes():
    api = FakeApi(turn=turn_result(reply="MV OCEAN STAR 82k dwt"))
    wa = FileWa()
    await run(wa_doc("pmx format please"), api, wa)
    [(content, attachment)] = api.turns
    assert content == "pmx format please"
    assert attachment == {"filename": "Recap MV OCEAN STAR.docx",
                          "mime_type": "application/vnd.openxmlformats-officedocument"
                                       ".wordprocessingml.document",
                          "data_b64": base64.b64encode(DOCX).decode()}
    assert "MV OCEAN STAR 82k dwt" in wa.texts[0][1]
    assert "Reading" not in wa.all_text  # no notice that would sit in the thread


@pytest.mark.asyncio
async def test_a_caption_less_document_sends_an_empty_instruction():
    api = FakeApi()
    await run(wa_doc(), api, FileWa())
    assert api.turns[0][0] == ""


@pytest.mark.asyncio
async def test_a_caption_that_looks_like_a_command_goes_to_the_agent():
    api = FakeApi()
    await run(wa_doc("new"), api, FileWa())
    assert api.turns[0][0] == "new"
    assert "set_session" not in api.calls


@pytest.mark.asyncio
async def test_every_download_asks_meta_for_a_fresh_url():
    """Meta's URL dies after five minutes, and a batch of turns can outlast
    that — so the URL is never carried from the webhook or reused."""
    api, wa = FakeApi(), FileWa()
    await run(wa_doc(mid="wamid.D1"), api, wa)
    await run(wa_doc(mid="wamid.D2"), api, wa)
    assert wa.media_calls == ["1234567890", "1234567890"]
    assert wa.downloads[0] != wa.downloads[1]


@pytest.mark.asyncio
async def test_an_unlinked_chat_never_asks_meta_for_the_file():
    api, wa = FakeApi(linked=False), FileWa()
    await run(wa_doc("describe"), api, wa)
    assert S.NOT_LINKED in wa.all_text
    assert wa.media_calls == [] and wa.downloads == []


@pytest.mark.asyncio
async def test_meta_reporting_an_oversize_file_skips_the_download():
    api, wa = FakeApi(), FileWa(file_size=str(MAX_BYTES + 1))
    await run(wa_doc(), api, wa)
    assert S.FILE_TOO_LARGE in wa.all_text
    assert wa.downloads == [] and "turn" not in api.calls


@pytest.mark.asyncio
async def test_a_hash_mismatch_is_unavailable_and_never_reaches_the_platform():
    for wa, message in (
        (FileWa(sha256="0" * 64), wa_doc()),                      # Graph's hash
        (FileWa(sha256=None), doc_msg(None, sha256="f" * 64)),    # the webhook's
    ):
        api = FakeApi()
        await run(message, api, wa)
        assert S.FILE_UNAVAILABLE in wa.all_text
        assert "turn" not in api.calls


@pytest.mark.asyncio
async def test_a_matching_hash_is_accepted_as_hex_or_base64():
    b64 = base64.b64encode(hashlib.sha256(DOCX).digest()).decode()
    for wa, message in ((FileWa(), wa_doc()),
                        (FileWa(sha256=b64), doc_msg(None, sha256=b64))):
        api = FakeApi()
        await run(message, api, wa)
        assert api.turns, "a matching hash must not block the file"


@pytest.mark.asyncio
async def test_a_graph_failure_says_the_file_was_not_processed():
    api, wa = FakeApi(), FileWa(media_error=WhatsAppError(100, 33, "Unsupported get request"))
    await run(wa_doc(), api, wa)
    assert S.FILE_UNAVAILABLE in wa.all_text
    assert "turn" not in api.calls


@pytest.mark.asyncio
async def test_an_old_platform_says_files_are_not_supported_yet():
    class OldApi(FakeApi):
        async def turn(self, chat_id, content, *, attachment=None, timeout):
            raise PlatformError(422, [{"type": "string_too_short", "loc": ["body", "content"]}])

    wa = FileWa()
    await run(wa_doc(), OldApi(), wa)
    assert S.FILES_NOT_SUPPORTED_YET in wa.all_text


@pytest.mark.asyncio
async def test_files_sent_faster_than_the_platform_takes_them_are_told_to_wait():
    class BusyApi(FakeApi):
        async def turn(self, chat_id, content, *, attachment=None, timeout):
            raise PlatformError(429, "too_many_attempts", retry_after=42)

    wa = FileWa()
    await run(wa_doc(), BusyApi(), wa)
    assert S.rate_limited("a minute", daily=False, file=True) in wa.all_text
    assert "*that file wasn't read*" in wa.all_text
    assert S.UNEXPECTED not in wa.all_text


@pytest.mark.asyncio
async def test_a_proxy_refusing_the_upload_says_the_file_was_not_passed_on():
    class ProxiedApi(FakeApi):
        async def turn(self, chat_id, content, *, attachment=None, timeout):
            raise PlatformError(413, "Request Entity Too Large")

    wa = FileWa()
    await run(wa_doc(), ProxiedApi(), wa)
    assert S.FILE_NOT_DELIVERED in wa.all_text
    assert S.FILE_TOO_LARGE not in wa.all_text


@pytest.mark.asyncio
async def test_the_access_token_never_reaches_errors_or_logs(caplog):
    """End to end over a real client: the download blows up with a transport
    error that names the URL and the auth header, the way a careless proxy
    or library message might. None of it may surface."""
    caplog.set_level(logging.DEBUG)
    token = "EAAG-wa-SECRET-access-token"

    def handler(req):
        if req.url.host == "graph.facebook.com" and req.method == "GET":
            return httpx.Response(200, json={"url": MEDIA_URL, "file_size": "10"})
        if req.url.host == "lookaside.fbsbx.com":
            raise httpx.ReadError(f"reset by peer: {req.url} {req.headers['authorization']}",
                                  request=req)
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    sent = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        wa = WhatsAppClient(token, "12345", http)
        real_send = wa.send_text

        async def send_text(to, body):
            sent.append(body)
            return await real_send(to, body)

        wa.send_text = send_text
        api = FakeApi()
        await run(wa_doc(), api, wa)

    assert S.FILE_UNAVAILABLE in sent
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert token not in logs and "lookaside" not in logs
    assert "ReadError" in logs  # the class, for whoever is debugging


def test_whatsapp_file_strings_state_the_cap_and_how_to_send():
    assert f"{MAX_BYTES // (1024 * 1024)} MB" in S.FILE_TOO_LARGE
    assert "PDF or Word file" in S.HELP and "caption" in S.HELP
    assert "PDF" in S.TEXT_ONLY and ".docx" in S.TEXT_ONLY
