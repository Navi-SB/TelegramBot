"""GET /api/spike — proves the one unverified platform assumption.

The whole webhook design rests on a self-invoked function surviving its caller
disconnecting. If it doesn't, every slow turn dies silently. Hit this once
after the first deploy, then check /api/spike_result.

If it fails, the fix is to swap bot/selfinvoke.py for a durable queue
(Upstash QStash publishes over HTTP and gives retries and dedupe for free);
nothing else in the design changes.
"""
from __future__ import annotations

import json
import time

from bot.asgi import Resp, json_endpoint
from bot.config import load
from bot.selfinvoke import fire


async def handle(req):
    cfg = load()
    started = time.time()
    body = json.dumps({"started": started, "sleep": 120}).encode()
    ok = await fire(f"{cfg.self_url}/api/spike_worker", body, cfg.internal_secret, cfg.vercel_bypass)
    return Resp(200, {
        "ok": ok,
        "started": started,
        "note": "Wait ~130s, then check the runtime logs for 'spike_survived'. "
                "If it never appears, the self-invoke does not outlive its caller.",
    })


app = json_endpoint(handle, methods=("GET",))
