"""Sleeps well past its caller's disconnect, then logs. See api/spike.py."""
from __future__ import annotations

import asyncio
import json
import time

from bot.asgi import Resp, json_endpoint
from bot.config import load
from bot.logging import log
from bot.selfinvoke import verify


async def handle(req):
    cfg = load()
    if not verify(req.headers, req.body, cfg.internal_secret):
        return Resp(401, {"ok": False})
    payload = json.loads(req.body or b"{}")
    await asyncio.sleep(min(int(payload.get("sleep", 120)), 240))
    log("spike_survived", duration_ms=int((time.time() - payload["started"]) * 1000))
    return Resp(200, {"ok": True})


app = json_endpoint(handle, methods=("POST",))
