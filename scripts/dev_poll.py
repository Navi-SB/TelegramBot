#!/usr/bin/env python3
"""Run the bot locally over long polling — no webhook, no tunnel, no Vercel.

This is also the VPS escape hatch, exercised. If Vercel ever stops suiting the
job, a systemd unit runs THIS file: it calls the same handle_update() the
serverless worker does, so the port is a deployment change rather than a
rewrite. Keeping it working is the point.

    python scripts/set_webhook.py --delete   # Telegram allows only one at a time
    python scripts/dev_poll.py
"""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from bot.config import load
from bot.dispatch import handle_update
from bot.platform.client import PlatformClient
from bot.telegram.api import TelegramClient


async def main() -> None:
    cfg = load()
    print(f"polling as {cfg.vercel_env}; platform={cfg.api_base}")
    offset = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(70.0)) as http:
        tg = TelegramClient(cfg.bot_token, http)
        api = PlatformClient(cfg.api_base, cfg.service_token, http)
        seen: set[int] = set()
        while True:
            try:
                updates = await tg._call("getUpdates", offset=offset, timeout=50,
                                         allowed_updates=["message", "callback_query"])
            except Exception as exc:
                print(f"  poll error: {type(exc).__name__}; retrying")
                await asyncio.sleep(3)
                continue
            for u in updates or []:
                offset = u["update_id"] + 1
                # getUpdates offsets are effectively exactly-once, so the
                # platform's dedupe isn't needed here — but a local set keeps
                # a restart from replaying.
                if u["update_id"] in seen:
                    continue
                seen.add(u["update_id"])
                print(f"  update {u['update_id']}")
                await handle_update(u, tg, api, turn_timeout=240)


asyncio.run(main())
