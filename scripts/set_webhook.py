#!/usr/bin/env python3
"""Point Telegram at this deployment. Run after the first deploy, and whenever
BOT_SELF_URL changes.

    python scripts/set_webhook.py            # set
    python scripts/set_webhook.py --delete   # unset (e.g. to run dev_poll.py)
    python scripts/set_webhook.py --info     # what Telegram currently thinks
"""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from bot.config import load
from bot.telegram.api import TelegramClient

# Narrowing allowed_updates shrinks both the attack surface and the invocation
# bill: no worker is woken for an edited_message or a poll answer.
ALLOWED = ["message", "callback_query", "my_chat_member"]

COMMANDS = [
    {"command": "agents", "description": "Pick which agent to talk to"},
    {"command": "agent", "description": "Switch to an agent by name, e.g. /agent PMX Short"},
    {"command": "short", "description": "Short description from a pasted vessel description"},
    {"command": "new", "description": "Start a fresh conversation"},
    {"command": "status", "description": "What I'm connected to"},
    {"command": "unlink", "description": "Disconnect this chat"},
    {"command": "help", "description": "How to use this bot"},
]


async def main() -> None:
    cfg = load()
    async with httpx.AsyncClient(timeout=15.0) as http:
        tg = TelegramClient(cfg.bot_token, http)
        me = await tg.get_me()
        print(f"bot: @{me['username']}  ({cfg.vercel_env})")

        if "--info" in sys.argv:
            info = await tg._call("getWebhookInfo")
            print(f"  url:     {info.get('url') or '(none)'}")
            print(f"  pending: {info.get('pending_update_count')}")
            if info.get("last_error_message"):
                print(f"  last error: {info['last_error_message']}")
            return

        if "--delete" in sys.argv:
            await tg.delete_webhook()
            print("  webhook deleted — long polling is now free to use")
            return

        url = f"{cfg.self_url}/api/telegram"
        await tg.set_webhook(url, cfg.webhook_secret, ALLOWED)
        await tg.set_my_commands(COMMANDS)
        print(f"  webhook: {url}")
        print(f"  updates: {', '.join(ALLOWED)}")
        print("  commands registered")


asyncio.run(main())
