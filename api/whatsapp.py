"""GET+POST /api/whatsapp — the Meta webhook. Same contract as /api/telegram:
authenticate, shape-check, hand off, 200 fast. The logic lives in
bot/whatsapp/webhook.py where the tests can reach it."""
from __future__ import annotations

from bot.asgi import json_endpoint
from bot.config import load
from bot.selfinvoke import fire
from bot.whatsapp.webhook import handle_webhook


async def handle(req):
    return await handle_webhook(req, load(), fire)


app = json_endpoint(handle, methods=("GET", "POST"))
