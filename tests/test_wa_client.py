"""The Cloud API client's retry ladder against realistic Graph responses."""
import pytest

from bot.whatsapp.api import WhatsAppClient, WhatsAppError, WhatsAppUndeliverable


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeHttp:
    """Yields the scripted responses in order; records the payloads sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.posts = []

    async def post(self, url, json=None, headers=None):
        self.posts.append(json)
        return FakeResponse(*self._responses.pop(0))


OK = (200, {"messages": [{"id": "wamid.SENT"}]})


def client(http):
    return WhatsAppClient("token", "12345", http)


@pytest.mark.asyncio
async def test_graph_transients_are_retried_not_dropped():
    """Meta's transient failures are HTTP 500 with body code 1 or 2 — the
    body code identifies them, and a chunk of a broker's answer must not be
    dropped over a wait-and-retry error."""
    for code in (1, 2):
        http = FakeHttp([(500, {"error": {"code": code, "message": "transient"}}), OK])
        wamid = await client(http).send_text("306912345678", "hello")
        assert wamid == "wamid.SENT"
        assert len(http.posts) == 2


@pytest.mark.asyncio
async def test_rate_limits_back_off_and_retry():
    http = FakeHttp([(429, {"error": {"code": 130429, "message": "throughput"}}), OK])
    assert await client(http).send_text("306912345678", "hi") == "wamid.SENT"


@pytest.mark.asyncio
async def test_undeliverable_raises_immediately_without_retry():
    http = FakeHttp([(400, {"error": {"code": 131026, "message": "undeliverable"}})])
    with pytest.raises(WhatsAppUndeliverable):
        await client(http).send_text("306912345678", "hi")
    assert len(http.posts) == 1  # the only blocked signal — never retried


@pytest.mark.asyncio
async def test_application_errors_raise_after_no_retry():
    http = FakeHttp([(400, {"error": {"code": 131009, "message": "bad param"}})])
    with pytest.raises(WhatsAppError):
        await client(http).send_text("306912345678", "hi")
    assert len(http.posts) == 1
