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
from bot.selfinvoke import fire_detailed


async def handle(req):
    cfg = load()
    started = time.time()
    body = json.dumps({"started": started, "sleep": 120}).encode()
    target = f"{cfg.self_url}/api/spike_worker"
    ok, reason = await fire_detailed(target, body, cfg.internal_secret, cfg.vercel_bypass)
    return Resp(200, {
        "ok": ok,
        # Without these two, a failure is indistinguishable from any other
        # failure — which is exactly what happened the first time this ran
        # against a BOT_SELF_URL that did not resolve.
        "target": target,
        "reason": reason,
        "started": started,
        "note": "Wait ~130s, then check the runtime logs for 'spike_survived'. "
                "If it never appears, the self-invoke does not outlive its caller.",
    })


app = json_endpoint(handle, methods=("GET",))
