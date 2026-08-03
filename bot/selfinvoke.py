"""Handing a slow update from the webhook to a worker.

Telegram waits ~60s for a 200 and re-delivers on timeout; sustained timeouts
trigger a retry storm where the same update arrives dozens of times. An agent
turn runs 30-90s, so it cannot happen inside that window.

`waitUntil` would be the obvious answer, but it has NO Python surface on
Vercel — it is an export of the @vercel/functions npm package. So the webhook
makes a second HTTPS request to /api/worker and deliberately abandons the read.

The connect and write MUST complete: that is what proves Vercel accepted the
request and dispatched an invocation. The read timing out is the normal path.

This rests on one platform behaviour — that the worker survives its caller
disconnecting. api/spike.py exists to prove that before it matters.
"""
from __future__ import annotations

import hashlib
import hmac
import time

import httpx

from .logging import log, log_exception

REPLAY_WINDOW_SECONDS = 300


def sign(body: bytes, ts: str, secret: str) -> str:
    return hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify(headers: dict[str, str], body: bytes, secret: str) -> bool:
    ts = headers.get("x-bot-ts", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > REPLAY_WINDOW_SECONDS:
        return False
    return hmac.compare_digest(headers.get("x-bot-sig", ""), sign(body, ts, secret))


async def fire_detailed(
    url: str, body: bytes, secret: str, bypass: str | None = None
) -> tuple[bool, str | None]:
    """(delivered, reason). The reason is the exception CLASS on failure —
    "ConnectError" says DNS or routing, "ConnectTimeout" says firewall, and
    both are things you can act on. Returning a bare False, as this did at
    first, tells you only that something went wrong somewhere."""
    ts = str(int(time.time()))
    headers = {
        "content-type": "application/json",
        "x-bot-ts": ts,
        "x-bot-sig": sign(body, ts, secret),
    }
    if bypass:
        # Survives Deployment Protection being switched on later.
        headers["x-vercel-protection-bypass"] = bypass

    timeout = httpx.Timeout(connect=3.0, write=3.0, read=0.4, pool=3.0)
    last = "unknown"
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(url, content=body, headers=headers)
            # Any status means it was DELIVERED — a 401 here would mean the
            # HMAC or a protection bypass is wrong, which is worth knowing but
            # is not a transport failure.
            return True, (f"http_{r.status_code}" if r.status_code >= 400 else None)
        except httpx.ReadTimeout:
            return True, None  # NORMAL: delivered, the worker is running
        except Exception as exc:
            last = type(exc).__name__
            if attempt == 2:
                log_exception("selfinvoke_failed", exc)
                return False, last
    return False, last


async def fire(url: str, body: bytes, secret: str, bypass: str | None = None) -> bool:
    delivered, _ = await fire_detailed(url, body, secret, bypass)
    return delivered
