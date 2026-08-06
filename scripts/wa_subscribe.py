#!/usr/bin/env python3
"""Subscribe the Meta app to the WABA and sanity-check the WhatsApp config.

Run AFTER the dashboard webhook is verified (Meta must be able to GET
/api/whatsapp first). Idempotent — safe to re-run, and required again when
switching from the test WABA to the production one.

    python scripts/wa_subscribe.py           # subscribe + show status
    python scripts/wa_subscribe.py --info    # show status only
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402

from bot.config import load  # noqa: E402
from bot.whatsapp.api import GRAPH  # noqa: E402


async def main() -> None:
    cfg = load()
    if not (cfg.wa_access_token and cfg.wa_waba_id):
        sys.exit("WHATSAPP_ACCESS_TOKEN and WHATSAPP_WABA_ID must be set.")
    headers = {"Authorization": f"Bearer {cfg.wa_access_token}"}
    base = f"{GRAPH}/{cfg.wa_graph_version}"

    async with httpx.AsyncClient(timeout=30.0) as http:
        if "--info" not in sys.argv:
            r = await http.post(f"{base}/{cfg.wa_waba_id}/subscribed_apps", headers=headers)
            print("subscribe:", r.status_code, r.text[:200])

        r = await http.get(f"{base}/{cfg.wa_waba_id}/subscribed_apps", headers=headers)
        print("subscribed apps:", r.status_code, r.text[:400])

        if cfg.wa_phone_number_id:
            r = await http.get(
                f"{base}/{cfg.wa_phone_number_id}",
                params={"fields": "display_phone_number,verified_name,quality_rating"},
                headers=headers,
            )
            print("number:", r.status_code, r.text[:400])


if __name__ == "__main__":
    asyncio.run(main())
