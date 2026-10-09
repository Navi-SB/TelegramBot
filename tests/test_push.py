"""POST /api/push — the one endpoint that makes the bot speak unprompted.

Everything else here answers a chat that just spoke to us. This one is asked by
VoyageCalc, so the tests are mostly about what it refuses: an unsigned caller,
a chat that never paired, a chat that blocked us, and a retry that would send
the same morning report twice.
"""
import json
import time

import pytest

import api.push as push
from bot.asgi import Req
from bot.selfinvoke import sign

SECRET = "push-secret"


class FakeApi:
    def __init__(self, link=None, fresh=True):
        self._link = link if link is not None else {"linked": True, "blocked": False}
        self._fresh = fresh
        self.claimed = []
        self.blocked = []

    async def get_link(self, chat_id):
        return self._link

    async def claim_event(self, event_id):
        self.claimed.append(event_id)
        return self._fresh

    async def mark_blocked(self, chat_id):
        self.blocked.append(chat_id)


class FakeTg:
    def __init__(self, error=None):
        self.sent = []
        self._error = error

    async def send_safe(self, chat_id, html, **kw):
        if self._error:
            raise self._error
        self.sent.append((chat_id, html))
        return 1


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setenv("BOT_PUSH_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "w")
    monkeypatch.setenv("BOT_INTERNAL_SECRET", "i")
    monkeypatch.setenv("BOT_SELF_URL", "https://bot.test")
    monkeypatch.setenv("VOYAGECALC_API_BASE", "https://api.test")
    monkeypatch.setenv("VOYAGECALC_SERVICE_TOKEN", "svc")


def _req(body: dict, *, secret=SECRET, ts=None):
    raw = json.dumps(body).encode()
    stamp = ts or str(int(time.time()))
    return Req(
        headers={"x-bot-ts": stamp, "x-bot-sig": sign(raw, stamp, secret)},
        body=raw,
    )


def _wire(monkeypatch, api=None, tg=None):
    api = api or FakeApi()
    tg = tg or FakeTg()
    monkeypatch.setattr(push, "PlatformClient", lambda *a, **kw: api)
    monkeypatch.setattr(push, "TelegramClient", lambda *a, **kw: tg)
    return api, tg


BODY = {"channel": "telegram", "chat_id": "chat-1", "text": "Yesterday: 3 fixtures.",
        "source": "Morning report", "idempotency_key": "run-1:send"}


@pytest.mark.asyncio
async def test_a_signed_push_reaches_the_chat(monkeypatch):
    api, tg = _wire(monkeypatch)
    res = await push.handle(_req(BODY))

    assert res.status == 200 and res.body["ok"] is True
    chat_id, html = tg.sent[0]
    assert chat_id == "chat-1" and "Yesterday: 3 fixtures." in html


@pytest.mark.asyncio
async def test_it_says_which_workflow_spoke(monkeypatch):
    """The provenance line is built here, never by the caller, so a pushed
    message can't be dressed up as the bot answering a question."""
    _, tg = _wire(monkeypatch)
    await push.handle(_req(BODY))
    assert tg.sent[0][1].startswith("🔁 <b>Morning report</b>")


@pytest.mark.asyncio
async def test_markdown_in_the_text_arrives_as_text_not_a_link(monkeypatch):
    """Hook-triggered text is attacker-supplied. A real anchor arriving from a
    bot the broker trusts is a phishing primitive."""
    _, tg = _wire(monkeypatch)
    await push.handle(_req({**BODY, "text": "[click here](http://evil.example)"}))
    assert "<a href" not in tg.sent[0][1]
    assert "click here" in tg.sent[0][1]


@pytest.mark.asyncio
async def test_an_unsigned_caller_is_turned_away(monkeypatch):
    _, tg = _wire(monkeypatch)
    res = await push.handle(_req(BODY, secret="wrong"))
    assert res.status == 401 and res.body["error"] == "bad_signature"
    assert tg.sent == []


@pytest.mark.asyncio
async def test_clock_drift_is_named_separately_from_a_bad_key(monkeypatch):
    _wire(monkeypatch)
    old = str(int(time.time()) - 10_000)
    res = await push.handle(_req(BODY, ts=old))
    assert res.status == 401 and res.body["error"] == "stale_timestamp"


@pytest.mark.asyncio
async def test_a_rotated_secret_still_verifies(monkeypatch):
    monkeypatch.setenv("BOT_PUSH_SECRET", "new-secret, push-secret")
    _wire(monkeypatch)
    assert (await push.handle(_req(BODY))).body["ok"] is True


@pytest.mark.asyncio
async def test_nothing_is_sent_to_a_chat_that_never_paired(monkeypatch):
    """This is the security boundary: without it, the push secret alone would
    let anyone message any chat id — which is exactly what the (channel,
    chat_id) design exists to prevent."""
    api, tg = _wire(monkeypatch, FakeApi(link={"linked": False}))
    res = await push.handle(_req(BODY))
    assert res.status == 200 and res.body["reason"] == "unlinked"
    assert tg.sent == []


@pytest.mark.asyncio
async def test_nothing_is_sent_to_a_chat_that_blocked_the_bot(monkeypatch):
    _, tg = _wire(monkeypatch, FakeApi(link={"linked": True, "blocked": True}))
    res = await push.handle(_req(BODY))
    assert res.body["reason"] == "blocked" and tg.sent == []


@pytest.mark.asyncio
async def test_a_retry_does_not_send_the_report_twice(monkeypatch):
    api, tg = _wire(monkeypatch, FakeApi(fresh=False))
    res = await push.handle(_req(BODY))

    assert res.body == {"ok": False, "reason": "duplicate_unknown", "certain": False}
    assert api.claimed == ["push:run-1:send"] and tg.sent == []


@pytest.mark.asyncio
async def test_blocking_us_mid_send_is_written_back(monkeypatch):
    from bot.telegram.api import TelegramError

    api, _ = _wire(monkeypatch, tg=FakeTg(error=TelegramError(403, "bot was blocked by the user")))
    res = await push.handle(_req(BODY))
    assert res.body["reason"] == "blocked"
    assert api.blocked == ["chat-1"], "every later push would 403 too"


@pytest.mark.asyncio
async def test_whatsapp_is_refused_for_now(monkeypatch):
    _, tg = _wire(monkeypatch)
    res = await push.handle(_req({**BODY, "channel": "whatsapp"}))
    assert res.body["reason"] == "unsupported_channel" and tg.sent == []


@pytest.mark.asyncio
async def test_it_is_closed_until_a_secret_is_configured(monkeypatch):
    monkeypatch.delenv("BOT_PUSH_SECRET", raising=False)
    _wire(monkeypatch)
    res = await push.handle(_req(BODY))
    assert res.status == 200 and res.body["reason"] == "not_configured" and res.body["certain"]


@pytest.mark.asyncio
async def test_a_long_report_is_split_rather_than_rejected(monkeypatch):
    """Telegram refuses anything over 4096 characters outright."""
    _, tg = _wire(monkeypatch)
    res = await push.handle(_req({**BODY, "text": "line\n" * 2000}))
    assert res.body["ok"] is True and len(tg.sent) > 1


@pytest.mark.asyncio
async def test_first_chunk_rate_limit_is_safe_to_retry(monkeypatch):
    from bot.telegram.api import TelegramError
    _wire(monkeypatch, tg=FakeTg(error=TelegramError(429, "Too many requests")))
    res = await push.handle(_req(BODY))
    assert res.body["reason"] == "rate_limited" and res.body["certain"] is True
    assert res.body["sent_chunks"] == 0


@pytest.mark.asyncio
async def test_partial_delivery_is_never_certified_safe_to_retry(monkeypatch):
    from bot.telegram.api import TelegramError
    class PartialTg(FakeTg):
        async def send_safe(self, chat_id, html, **kw):
            assert kw["retries"] == 0
            if self.sent:
                raise TelegramError(429, "Too many requests")
            self.sent.append((chat_id, html))
            return 1
    _wire(monkeypatch, tg=PartialTg())
    res = await push.handle(_req({**BODY, "text": "line\n" * 2000}))
    assert res.body["certain"] is False and res.body["sent_chunks"] == 1


@pytest.mark.asyncio
async def test_server_error_is_unknown_even_on_first_chunk(monkeypatch):
    from bot.telegram.api import TelegramError
    _wire(monkeypatch, tg=FakeTg(error=TelegramError(502, "Unavailable")))
    res = await push.handle(_req(BODY))
    assert res.body["certain"] is False


@pytest.mark.asyncio
async def test_report_too_large_is_rejected_without_truncation(monkeypatch):
    _, tg = _wire(monkeypatch)
    res = await push.handle(_req({**BODY, "text": "x" * (push.MAX_TEXT + 1)}))
    assert res.status == 400 and res.body["error"] == "message_too_long"
    assert res.body["certain"] and not tg.sent


@pytest.mark.asyncio
async def test_send_message_does_not_repeat_after_a_transport_timeout(monkeypatch):
    import httpx
    from bot.telegram.api import TelegramClient
    calls = []
    async def request(req):
        calls.append(req)
        raise httpx.ReadTimeout("response lost")
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        tg = TelegramClient("test-token", client)
        with pytest.raises(httpx.ReadTimeout):
            await tg.send_safe("chat", "report")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_link", "claim_event"])
async def test_platform_failure_before_send_is_a_definite_refusal(monkeypatch, method):
    from bot.platform.client import PlatformUnavailable
    api, tg = _wire(monkeypatch)
    async def unavailable(*args, **kwargs):
        raise PlatformUnavailable(503, "offline")
    monkeypatch.setattr(api, method, unavailable)
    res = await push.handle(_req(BODY))
    assert not res.body["ok"] and res.body["certain"]
    assert res.body["reason"] == "bridge_unavailable" and not tg.sent
