"""Report files at the HTTP level — the platform's byte route, the file name,
and Telegram's multipart sends with their no-resend contract.

Real httpx clients over httpx.MockTransport, as in test_attachments.py, so
what is tested is the request that would really go out.
"""
import json

import httpx
import pytest

from bot.platform.client import (
    CONFIRM_TIMEOUT, FILE_MAX_BYTES, PlatformClient, PlatformError, PlatformUnavailable,
    filename_from,
)
from bot.telegram.api import CAPTION_LIMIT, TelegramClient, TelegramError
from bot.telegram.channel import TelegramChannel

TOKEN = "123456:SECRET-bot-token-DO-NOT-LOG"
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 4000
DISPOSITION = ("attachment; filename=\"Weekly Period Rundown 2026-11-10 093000.png\"; "
               "filename*=UTF-8''Weekly%20Period%20Rundown%202026-11-10%20093000.png")


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# POST /api/bot/files
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_file_is_fetched_by_post_with_the_chat_and_export_id():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=PNG, headers={
            "content-type": "image/png", "content-disposition": DISPOSITION,
            "cache-control": "no-store"})

    async with client(handler) as http:
        api = PlatformClient("http://platform.test", "tok", http, channel="whatsapp")
        data, mime, name = await api.file("306912345678", 7)
    assert (data, mime, name) == (PNG, "image/png", "Weekly Period Rundown 2026-11-10 093000.png")
    [req] = seen
    assert req.method == "POST" and req.url.path == "/api/bot/files"
    # the phone number rides the body, never the query string
    assert req.url.query == b""
    assert json.loads(req.content) == {"channel": "whatsapp", "chat_id": "306912345678",
                                       "export_id": 7}
    assert req.headers["x-service-token"] == "tok"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,detail", [
    (404, "That export is not on this desk."),
    (410, "This file was removed after 30 days; export again."),
])
async def test_a_missing_or_removed_file_keeps_the_platforms_sentence(status, detail):
    def handler(request):
        return httpx.Response(status, json={"detail": detail})

    async with client(handler) as http:
        with pytest.raises(PlatformError) as caught:
            await PlatformClient("http://platform.test", "tok", http).file("1", 7)
    assert (caught.value.status, caught.value.detail) == (status, detail)


@pytest.mark.asyncio
async def test_an_unreachable_platform_is_unavailable_not_a_refusal():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    async with client(handler) as http:
        with pytest.raises(PlatformUnavailable):
            await PlatformClient("http://platform.test", "tok", http).file("1", 7)


@pytest.mark.asyncio
async def test_a_body_past_the_cap_is_abandoned():
    pulled = []

    async def body():
        for _ in range(FILE_MAX_BYTES // 1_000_000 + 5):
            pulled.append(1)
            yield b"x" * 1_000_000

    def handler(request):
        return httpx.Response(200, content=body(), headers={"content-type": "image/png"})

    async with client(handler) as http:
        with pytest.raises(PlatformError) as caught:
            await PlatformClient("http://platform.test", "tok", http).file("1", 7)
    assert caught.value.status == 413
    assert len(pulled) <= FILE_MAX_BYTES // 1_000_000 + 1


@pytest.mark.asyncio
async def test_a_declared_length_past_the_cap_is_refused_before_reading():
    def handler(request):
        return httpx.Response(200, content=b"x", headers={
            "content-type": "application/pdf", "content-length": str(FILE_MAX_BYTES + 1)})

    async with client(handler) as http:
        with pytest.raises(PlatformError) as caught:
            await PlatformClient("http://platform.test", "tok", http).file("1", 7)
    assert caught.value.status == 413


@pytest.mark.asyncio
async def test_confirm_waits_long_enough_for_a_render():
    """An export's approve renders the report before it answers, which can
    pass the worker client's 30 s default."""
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"status": "applied"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 timeout=httpx.Timeout(30.0)) as http:
        await PlatformClient("http://platform.test", "tok", http).confirm("1", "p1", "approve")
    assert CONFIRM_TIMEOUT == 90.0
    assert timeouts[0]["read"] == 90.0


@pytest.mark.parametrize("disposition,mime,name", [
    # filename* (RFC 5987) wins: it carries the real, non-ASCII name
    ("attachment; filename=\"Rundown _ 2026.pdf\"; filename*=UTF-8''Rundown%20%CE%91%202026.pdf",
     "application/pdf", "Rundown Α 2026.pdf"),
    ("attachment; filename=\"Plain name.pdf\"", "application/pdf", "Plain name.pdf"),
    ("attachment; filename=bare.png", "image/png", "bare.png"),
    # an undecodable filename* falls back to filename
    ("attachment; filename*=UTF-8''%FF%FE.png; filename=\"ok.png\"", "image/png", "ok.png"),
    # nothing usable: a name the user can still open
    (None, "image/png", "Period Rundown.png"),
    ("attachment", "application/pdf", "Period Rundown.pdf"),
    # never a path
    ("attachment; filename*=UTF-8''..%2F..%2Fsecret.pdf", "application/pdf", "secret.pdf"),
])
def test_the_file_name_comes_from_content_disposition(disposition, mime, name):
    assert filename_from(disposition, mime) == name


def test_a_very_long_name_keeps_its_extension():
    name = filename_from("attachment; filename=\"" + "x" * 300 + ".pdf\"", "application/pdf")
    assert len(name) == 200 and name.endswith(".pdf")


# ---------------------------------------------------------------------------
# Telegram sendPhoto / sendDocument
# ---------------------------------------------------------------------------
def form_parts(request) -> dict[str, bytes]:
    """The multipart body, part name → content (good enough for asserting)."""
    ctype = request.headers["content-type"]
    assert ctype.startswith("multipart/form-data")
    boundary = ctype.split("boundary=")[1].encode()
    parts = {}
    for raw in request.content.split(b"--" + boundary):
        head, _, body = raw.partition(b"\r\n\r\n")
        if b'name="' not in head:
            continue
        key = head.split(b'name="')[1].split(b'"')[0].decode()
        parts[key] = body.rsplit(b"\r\n", 1)[0]
    return parts


@pytest.mark.asyncio
async def test_send_photo_posts_multipart_with_an_escaped_caption():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 55}})

    async with client(handler) as http:
        tg = TelegramClient(TOKEN, http)
        mid = await tg.send_photo(42, PNG, filename="Rundown.png", caption="Rundown <W45> & co")
    assert mid == 55
    [req] = seen
    assert req.url.path.endswith("/sendPhoto")
    parts = form_parts(req)
    assert parts["chat_id"] == b"42"
    assert parts["photo"] == PNG
    assert parts["caption"].decode() == "Rundown &lt;W45&gt; &amp; co"
    assert parts["parse_mode"] == b"HTML"
    assert b'filename="Rundown.png"' in req.content


@pytest.mark.asyncio
async def test_send_document_posts_the_file_under_its_name():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 56}})

    async with client(handler) as http:
        await TelegramClient(TOKEN, http).send_document(42, b"%PDF-1.7", filename="Rundown.pdf")
    [req] = seen
    assert req.url.path.endswith("/sendDocument")
    parts = form_parts(req)
    assert parts["document"] == b"%PDF-1.7"
    assert "caption" not in parts and "parse_mode" not in parts
    assert b"application/pdf" in req.content


@pytest.mark.asyncio
async def test_a_caption_is_clipped_to_telegrams_limit_before_escaping():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with client(handler) as http:
        await TelegramClient(TOKEN, http).send_photo(1, PNG, filename="a.png",
                                                     caption="&" * 2000)
    caption = form_parts(seen[0])["caption"].decode()
    # what Telegram counts is the text after parsing, so count that
    import html
    shown = html.unescape(caption)
    assert shown == "&" * (CAPTION_LIMIT - 1) + "…"
    assert caption.endswith("&amp;…")  # no entity cut in half


def test_an_emoji_counts_twice_toward_the_caption_limit():
    from bot.telegram.api import _clip_utf16

    clipped = _clip_utf16("🚢" * 600, CAPTION_LIMIT)
    assert len(clipped.encode("utf-16-le")) // 2 <= CAPTION_LIMIT
    assert clipped.endswith("…")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "5xx"])
async def test_a_file_send_is_never_retried_after_it_may_have_landed(failure):
    """A transport error or a 5xx can come after Telegram already posted the
    file; sending it again would put the report in the chat twice."""
    calls = []

    def handler(request):
        calls.append(1)
        if failure == "transport":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(502, json={"ok": False, "error_code": 502,
                                         "description": "Bad Gateway"})

    async with client(handler) as http:
        with pytest.raises((httpx.ReadTimeout, TelegramError)):
            await TelegramClient(TOKEN, http).send_photo(1, PNG, filename="a.png")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_rate_limited_file_send_waits_and_retries(monkeypatch):
    calls = []

    async def no_sleep(_):
        pass

    monkeypatch.setattr("bot.telegram.api.asyncio.sleep", no_sleep)

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429,
                                             "description": "Too Many Requests",
                                             "parameters": {"retry_after": 1}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    async with client(handler) as http:
        assert await TelegramClient(TOKEN, http).send_document(1, b"%PDF", filename="a.pdf") == 9
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_photo_telegram_refuses_goes_as_a_document_instead():
    """A 400 means nothing was posted — e.g. an image taller than a photo may
    be — so the same file goes through as a document."""
    methods = []

    def handler(request):
        methods.append(request.url.path.rsplit("/", 1)[-1])
        if methods[-1] == "sendPhoto":
            return httpx.Response(400, json={"ok": False, "error_code": 400,
                                             "description": "Bad Request: PHOTO_INVALID_DIMENSIONS"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})

    async with client(handler) as http:
        ch = TelegramChannel(TelegramClient(TOKEN, http))
        await ch.send_photo("1", PNG, filename="a.png", caption="cap")
    assert methods == ["sendPhoto", "sendDocument"]


@pytest.mark.asyncio
async def test_a_photo_that_failed_on_a_5xx_is_not_sent_again_as_a_document():
    methods = []

    def handler(request):
        methods.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(500, json={"ok": False, "error_code": 500,
                                         "description": "Internal Server Error"})

    async with client(handler) as http:
        ch = TelegramChannel(TelegramClient(TOKEN, http))
        with pytest.raises(TelegramError):
            await ch.send_photo("1", PNG, filename="a.png")
    assert methods == ["sendPhoto"]


@pytest.mark.asyncio
async def test_the_bot_token_never_appears_in_a_file_send_failure(caplog):
    import logging

    def handler(request):
        return httpx.Response(400, json={"ok": False, "error_code": 400,
                                         "description": "Bad Request: chat not found"})

    async with client(handler) as http:
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(TelegramError) as caught:
                await TelegramClient(TOKEN, http).send_document(1, b"%PDF", filename="a.pdf")
    assert TOKEN not in str(caught.value) and TOKEN not in caplog.text
