"""File downloads at the HTTP level — the cap, the token, the wire format.

Real httpx clients over httpx.MockTransport, so what is tested is what runs:
the stream really is abandoned part-way, and the exceptions really are the
ones a transport would raise (whose messages can carry the URL).
"""
import base64
import hashlib
import json
import logging

import httpx
import pytest

from bot import strings as S
from bot.core.attachments import (
    MAX_BYTES, Attachment, AttachmentTooLarge, AttachmentUnavailable, read_capped,
    sha256_matches, to_payload,
)
from bot.dispatch import handle_update
from bot.platform.client import PlatformClient
from bot.telegram.api import TelegramClient
from bot.telegram.channel import TelegramChannel
from tests.test_dispatch import FakeApi, doc

TOKEN = "123456:SECRET-bot-token-DO-NOT-LOG"
URL = "https://files.example/doc.pdf"


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Drip:
    """An endless-ish body that counts how much of itself was pulled."""

    def __init__(self, chunk=b"x" * 1_000_000, chunks=50):
        self.chunk, self.chunks, self.pulled = chunk, chunks, 0

    async def __aiter__(self):
        for _ in range(self.chunks):
            self.pulled += 1
            yield self.chunk


# ---------------------------------------------------------------------------
# the capped stream
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_body_with_no_declared_size_is_abandoned_once_past_the_cap():
    body = Drip()
    async with client(lambda req: httpx.Response(200, content=body)) as http:
        with pytest.raises(AttachmentTooLarge):
            await read_capped(http, URL, MAX_BYTES)
    assert body.pulled <= 6  # 5 MB cap, 1 MB chunks: stopped, not drained


@pytest.mark.asyncio
async def test_a_content_length_that_lies_low_does_not_get_past_the_cap():
    body = Drip()
    handler = lambda req: httpx.Response(200, headers={"content-length": "1000"}, content=body)
    async with client(handler) as http:
        with pytest.raises(AttachmentTooLarge):
            await read_capped(http, URL, MAX_BYTES)
    assert body.pulled <= 6


@pytest.mark.asyncio
async def test_a_declared_content_length_over_the_cap_is_refused_before_reading():
    body = Drip()
    handler = lambda req: httpx.Response(
        200, headers={"content-length": str(MAX_BYTES + 1)}, content=body)
    async with client(handler) as http:
        with pytest.raises(AttachmentTooLarge):
            await read_capped(http, URL, MAX_BYTES)
    assert body.pulled == 0


@pytest.mark.asyncio
async def test_a_file_under_the_cap_comes_back_whole():
    data = b"%PDF-1.4\n" + bytes(range(256)) * 100
    async with client(lambda req: httpx.Response(200, content=data)) as http:
        assert await read_capped(http, URL, MAX_BYTES) == data


@pytest.mark.asyncio
async def test_error_statuses_empty_bodies_and_plain_http_are_unavailable():
    async with client(lambda req: httpx.Response(404, content=b"gone")) as http:
        with pytest.raises(AttachmentUnavailable, match="download_404"):
            await read_capped(http, URL, MAX_BYTES)
    async with client(lambda req: httpx.Response(200, content=b"")) as http:
        with pytest.raises(AttachmentUnavailable, match="empty"):
            await read_capped(http, URL, MAX_BYTES)
    seen = []
    async with client(lambda req: seen.append(req) or httpx.Response(200, content=b"x")) as http:
        with pytest.raises(AttachmentUnavailable, match="not_https"):
            await read_capped(http, "http://files.example/doc.pdf", MAX_BYTES,
                              headers={"Authorization": "Bearer t"})
    assert seen == []  # a bearer token never goes out over plain http


# ---------------------------------------------------------------------------
# Telegram: getFile, then the tokenised download URL
# ---------------------------------------------------------------------------
def telegram_handler(file_bytes=b"%PDF-1.7 hello", *, file_size=None, fail_download=False):
    requests = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        path = req.url.path
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {
                "file_id": "BQACAgQ", "file_path": "documents/file_7.pdf",
                "file_size": len(file_bytes) if file_size is None else file_size}})
        if path.startswith(f"/file/bot{TOKEN}/"):
            if fail_download:
                # What a real transport failure looks like: the message
                # names the URL, token and all.
                raise httpx.ConnectError(f"All connection attempts failed for {req.url}",
                                         request=req)
            return httpx.Response(200, content=file_bytes)
        if path.endswith(("/sendMessage", "/editMessageText")):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})
        if path.endswith("/sendChatAction"):
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(404, json={"ok": False, "error_code": 404,
                                         "description": "Not Found"})

    return handler, requests


@pytest.mark.asyncio
async def test_telegram_fetches_the_file_through_getfile_and_its_path():
    handler, requests = telegram_handler(b"%PDF-1.7 hello")
    async with client(handler) as http:
        ch = TelegramChannel(TelegramClient(TOKEN, http))
        data = await ch.fetch_attachment(Attachment(ref="BQACAgQ"), MAX_BYTES)
    assert data == b"%PDF-1.7 hello"
    assert [r.url.path for r in requests] == [
        f"/bot{TOKEN}/getFile", f"/file/bot{TOKEN}/documents/file_7.pdf"]


@pytest.mark.asyncio
async def test_getfile_reporting_an_oversize_file_skips_the_download():
    handler, requests = telegram_handler(file_size=MAX_BYTES + 1)
    async with client(handler) as http:
        ch = TelegramChannel(TelegramClient(TOKEN, http))
        with pytest.raises(AttachmentTooLarge):
            await ch.fetch_attachment(Attachment(ref="BQACAgQ"), MAX_BYTES)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_the_bot_token_never_appears_in_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    handler, _ = telegram_handler(fail_download=True)
    async with client(handler) as http:
        ch = TelegramChannel(TelegramClient(TOKEN, http))
        with pytest.raises(AttachmentUnavailable) as info:
            await ch.fetch_attachment(Attachment(ref="BQACAgQ"), MAX_BYTES)
        err = info.value
        assert TOKEN not in str(err) and TOKEN not in repr(err)
        assert err.__cause__ is None and err.__suppress_context__  # no chained URL

        # And end to end, through the dispatch that logs the failure.
        sent = []
        tg = TelegramClient(TOKEN, http)
        real_send = tg.send_message

        async def send_message(chat_id, text, **kw):
            sent.append(text)
            return await real_send(chat_id, text, **kw)

        tg.send_message = send_message
        await handle_update(doc("describe"), tg, FakeApi(), turn_timeout=30)

    assert S.READING_FILE in sent
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in logs
    assert "attachment_failed" in logs and "ConnectError" in logs


# ---------------------------------------------------------------------------
# the wire format agreed with VoyageCalc
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_text_turn_sends_no_attachment_key_and_a_file_turn_sends_one():
    bodies = []

    def handler(req):
        bodies.append(req.content)
        return httpx.Response(200, json={"reply": "ok"})

    async with client(handler) as http:
        api = PlatformClient("https://api.test", "svc", http)
        await api.turn("1", "hello", timeout=5)
        payload = to_payload(Attachment(ref="f", file_name="recap.docx", mime_hint=None),
                             b"PK\x03\x04docx")
        await api.turn("1", "", attachment=payload, timeout=5)

    text_turn, file_turn = (json.loads(b) for b in bodies)
    assert text_turn == {"channel": "telegram", "chat_id": "1", "content": "hello"}
    assert file_turn == {"channel": "telegram", "chat_id": "1", "content": "",
                         "attachment": {"filename": "recap.docx", "mime_type": None,
                                        "data_b64": base64.b64encode(b"PK\x03\x04docx").decode()}}


def test_a_very_long_file_name_is_trimmed_but_keeps_its_extension():
    payload = to_payload(Attachment(ref="f", file_name="A" * 400 + ".pdf",
                                    mime_hint="application/pdf"), b"%PDF")
    assert len(payload["filename"]) == 255
    assert payload["filename"].endswith(".pdf")
    assert to_payload(Attachment(ref="f"), b"x")["filename"] is None


def test_a_hash_matches_as_hex_or_base64_and_nothing_else():
    data = b"%PDF-1.7 recap"
    digest = hashlib.sha256(data).digest()
    assert sha256_matches(digest.hex(), data)
    assert sha256_matches(digest.hex().upper(), data)
    assert sha256_matches(base64.b64encode(digest).decode(), data)
    assert sha256_matches(base64.urlsafe_b64encode(digest).decode().rstrip("="), data)
    assert not sha256_matches(hashlib.sha256(b"other").hexdigest(), data)
    assert not sha256_matches("not a hash at all", data)
    assert not sha256_matches("", data)
