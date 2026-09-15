"""The Cloud API client's retry ladder against realistic Graph responses."""
import httpx
import pytest

from bot.core.attachments import MAX_BYTES, AttachmentTooLarge
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


# ---------------------------------------------------------------------------
# media (NAV-81): GET /{media_id} for a fresh URL, then a Bearer download
# ---------------------------------------------------------------------------
MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1234567890"


def graph(handler) -> tuple[WhatsAppClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WhatsAppClient("wa-token", "12345", http, "v26.0"), http


@pytest.mark.asyncio
async def test_get_media_asks_graph_for_the_id_with_the_bearer_token():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"url": MEDIA_URL, "mime_type": "application/pdf",
                                         "sha256": "abc", "file_size": "303833",
                                         "id": "1234567890", "messaging_product": "whatsapp"})

    wa, http = graph(handler)
    async with http:
        media = await wa.get_media("1234567890")
    assert media["url"] == MEDIA_URL
    [req] = seen
    assert (req.method, str(req.url)) == ("GET", "https://graph.facebook.com/v26.0/1234567890")
    assert req.headers["authorization"] == "Bearer wa-token"


@pytest.mark.asyncio
async def test_get_media_retries_a_graph_transient_once():
    replies = [httpx.Response(500, json={"error": {"code": 2, "message": "service"}}),
               httpx.Response(200, json={"url": MEDIA_URL})]
    wa, http = graph(lambda req: replies.pop(0))
    async with http:
        assert (await wa.get_media("1234567890"))["url"] == MEDIA_URL


@pytest.mark.asyncio
async def test_a_malformed_media_id_never_becomes_a_url():
    seen = []
    wa, http = graph(lambda req: seen.append(req) or httpx.Response(200, json={}))
    async with http:
        with pytest.raises(WhatsAppError):
            await wa.get_media("../12345/messages")
        with pytest.raises(WhatsAppError):
            await wa.get_media("1234567890\n")
    assert seen == []


@pytest.mark.asyncio
async def test_the_media_download_carries_the_bearer_token_and_is_capped():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, content=b"%PDF-1.7 recap")

    wa, http = graph(handler)
    async with http:
        assert await wa.download_media(MEDIA_URL, MAX_BYTES) == b"%PDF-1.7 recap"
        with pytest.raises(AttachmentTooLarge):
            await wa.download_media(MEDIA_URL, 5)
    assert seen[0].headers["authorization"] == "Bearer wa-token"
