"""The Meta webhook contract: handshake, signature, status filtering."""
import hashlib
import hmac
import json

import pytest

from bot.asgi import Req
from bot.whatsapp.webhook import handle_webhook, is_actionable

APP_SECRET = "app-secret-for-tests"
VERIFY = "verify-token-for-tests"


class Cfg:
    wa_verify_token = VERIFY
    wa_app_secret = APP_SECRET
    self_url = "https://bots.example"
    internal_secret = "internal"
    vercel_bypass = None


class Fire:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def __call__(self, url, body, secret, bypass=None):
        self.calls.append((url, body))
        return self.ok


def signed(body: bytes) -> dict[str, str]:
    sig = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"x-hub-signature-256": f"sha256={sig}"}


def messages_body() -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "messages": [{"from": "306912345678", "id": "wamid.X", "type": "text",
                          "text": {"body": "hi"}}]}}]}],
    }).encode()


def statuses_body() -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "statuses": [{"id": "wamid.OUT", "status": "read"}]}}]}],
    }).encode()


# ---------------------------------------------------------------------------
# GET — the subscription handshake
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_challenge_is_echoed_as_plain_text():
    req = Req(method="GET", query={"hub.mode": "subscribe",
                                   "hub.verify_token": VERIFY,
                                   "hub.challenge": "1158201444"})
    res = await handle_webhook(req, Cfg(), Fire())
    assert res.status == 200
    assert res.text == "1158201444"  # byte-for-byte, not JSON-wrapped


@pytest.mark.asyncio
async def test_a_wrong_verify_token_is_refused_without_detail():
    req = Req(method="GET", query={"hub.mode": "subscribe",
                                   "hub.verify_token": "guess",
                                   "hub.challenge": "x"})
    res = await handle_webhook(req, Cfg(), Fire())
    assert res.status == 403
    assert res.text is None


# ---------------------------------------------------------------------------
# POST — signature first, then the status filter, then the hand-off
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_signed_message_payload_wakes_the_worker():
    body = messages_body()
    fire = Fire()
    res = await handle_webhook(Req(method="POST", body=body, headers=signed(body)),
                               Cfg(), fire)
    assert res.status == 200
    (url, fired_body), = fire.calls
    assert url.endswith("/api/wa_worker")
    assert fired_body == body  # the raw bytes, untouched


@pytest.mark.asyncio
async def test_a_bad_signature_is_401_and_no_worker_runs():
    body = messages_body()
    fire = Fire()
    for headers in ({}, {"x-hub-signature-256": "sha256=" + "0" * 64},
                    {"x-hub-signature-256": "nonsense"}):
        res = await handle_webhook(Req(method="POST", body=body, headers=headers),
                                   Cfg(), fire)
        assert res.status == 401
    assert fire.calls == []


@pytest.mark.asyncio
async def test_a_tampered_body_fails_the_signature():
    body = messages_body()
    headers = signed(body)
    res = await handle_webhook(Req(method="POST", body=body + b" ", headers=headers),
                               Cfg(), Fire())
    assert res.status == 401


@pytest.mark.asyncio
async def test_delivery_receipts_never_burn_a_worker_invocation():
    """Three receipts arrive per outbound message, on the same webhook field —
    they cannot be unsubscribed, only filtered."""
    body = statuses_body()
    fire = Fire()
    res = await handle_webhook(Req(method="POST", body=body, headers=signed(body)),
                               Cfg(), fire)
    assert res.status == 200
    assert fire.calls == []


@pytest.mark.asyncio
async def test_malformed_json_is_swallowed_with_200():
    body = b"not json"
    res = await handle_webhook(Req(method="POST", body=body, headers=signed(body)),
                               Cfg(), Fire())
    assert res.status == 200


@pytest.mark.asyncio
async def test_a_failed_handoff_returns_500_so_meta_retries():
    body = messages_body()
    res = await handle_webhook(Req(method="POST", body=body, headers=signed(body)),
                               Cfg(), Fire(ok=False))
    assert res.status == 500


def test_actionability_shapes():
    assert is_actionable(json.loads(messages_body()))
    assert not is_actionable(json.loads(statuses_body()))
    assert not is_actionable({"object": "page", "entry": []})
    assert not is_actionable({"object": "whatsapp_business_account", "entry": []})
