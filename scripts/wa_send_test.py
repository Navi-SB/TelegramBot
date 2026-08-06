#!/usr/bin/env python3
"""One-off smoke test: send a text to a verified recipient number.

Before business verification, the test number can only message the (up to 5)
recipients verified in the dashboard. The recipient must have messaged the
bot within 24h OR this must be the pre-approved hello_world territory — for a
plain text send, have the recipient text the bot number first.

    python scripts/wa_send_test.py 3069XXXXXXXX "hello from the bot"
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402

from bot.config import load  # noqa: E402
from bot.whatsapp.api import WhatsAppClient  # noqa: E402


async def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    to = sys.argv[1]
    body = sys.argv[2] if len(sys.argv) > 2 else "✅ Tropis WhatsApp bot smoke test."

    cfg = load()
    if not cfg.whatsapp_ready:
        sys.exit("WhatsApp env vars missing — see .env.example.")

    async with httpx.AsyncClient(timeout=30.0) as http:
        wa = WhatsAppClient(cfg.wa_access_token, cfg.wa_phone_number_id, http,
                            cfg.wa_graph_version)
        wamid = await wa.send_text(to, body)
        print("sent:", wamid)


if __name__ == "__main__":
    asyncio.run(main())
