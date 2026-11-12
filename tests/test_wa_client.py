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
        self.urls = []
        self.forms = []  # (data, files) of a multipart post

    async def post(self, url, json=None, headers=None, data=None, files=None):
        self.posts.append(json)
        self.urls.append(url)
        self.forms.append((data, files))
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


# ---------------------------------------------------------------------------
# outbound files (NAV-TBD-82): upload to /media, then a message by media id
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_upload_goes_to_media_as_multipart_and_returns_the_id():
    http = FakeHttp([(200, {"id": "4490709327384033"})])
    media_id = await client(http).upload_media(b"%PDF-1.7", "application/pdf", "Rundown.pdf")
    assert media_id == "4490709327384033"
    assert http.urls == ["https://graph.facebook.com/v26.0/12345/media"]
    data, files = http.forms[0]
    assert data == {"messaging_product": "whatsapp", "type": "application/pdf"}
    assert files == {"file": ("Rundown.pdf", b"%PDF-1.7", "application/pdf")}


@pytest.mark.asyncio
async def test_an_upload_rides_the_same_retry_ladder():
    http = FakeHttp([(500, {"error": {"code": 2, "message": "transient"}}), (200, {"id": "77"})])
    assert await client(http).upload_media(b"x", "image/png", "a.png") == "77"
    assert len(http.urls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"id": ""}, {"id": "../../me"}, {"id": "a" * 200}])
async def test_a_malformed_media_id_is_refused(body):
    """It goes into the next message as is — never trust its shape."""
    http = FakeHttp([(200, body)])
    with pytest.raises(WhatsAppError):
        await client(http).upload_media(b"x", "image/png", "a.png")


@pytest.mark.asyncio
async def test_an_image_message_names_the_media_id_and_caption():
    http = FakeHttp([OK])
    assert await client(http).send_image("306912345678", "77", "Weekly rundown") == "wamid.SENT"
    assert http.urls == ["https://graph.facebook.com/v26.0/12345/messages"]
    assert http.posts[0] == {"messaging_product": "whatsapp", "recipient_type": "individual",
                             "to": "306912345678", "type": "image",
                             "image": {"id": "77", "caption": "Weekly rundown"}}


@pytest.mark.asyncio
async def test_a_document_message_carries_its_file_name():
    http = FakeHttp([OK])
    await client(http).send_document("306912345678", "78", "Rundown.pdf")
    assert http.posts[0]["type"] == "document"
    assert http.posts[0]["document"] == {"id": "78", "filename": "Rundown.pdf"}


@pytest.mark.asyncio
async def test_a_caption_is_clipped_to_the_cloud_api_limit():
    http = FakeHttp([OK])
    await client(http).send_image("306912345678", "77", "x" * 3000)
    assert len(http.posts[0]["image"]["caption"]) == 1024


@pytest.mark.asyncio
async def test_a_blocked_recipient_on_a_media_send_is_undeliverable():
    http = FakeHttp([(400, {"error": {"code": 131026, "message": "undeliverable"}})])
    with pytest.raises(WhatsAppUndeliverable):
        await client(http).send_document("306912345678", "78", "Rundown.pdf")


@pytest.mark.asyncio
async def test_a_real_upload_is_multipart_with_the_bearer_token():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "4490709327384033"})

    wa, http = graph(handler)
    async with http:
        assert await wa.upload_media(b"%PDF-1.7", "application/pdf", "Rundown.pdf") \
            == "4490709327384033"
    [req] = seen
    assert req.url.path == "/v26.0/12345/media"
    assert req.headers["authorization"] == "Bearer wa-token"
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="messaging_product"' in req.content and b"whatsapp" in req.content
    assert b'filename="Rundown.pdf"' in req.content and b"%PDF-1.7" in req.content
