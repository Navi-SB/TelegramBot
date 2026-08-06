"""handle_update() — the Telegram entry point, kept as a stable seam.

The flows themselves live in bot/core/dispatch.py, channel-neutral; the
Telegram-shaped parts (parsing, HTML rendering, keyboards, the placeholder
edit) live in bot/telegram/channel.py. This shim preserves the original
signature so api/worker.py, scripts/dev_poll.py and the test suite are
untouched by the extraction — the tests pin the extracted behaviour.
"""
from __future__ import annotations

from typing import Any

from .core.dispatch import Inbound, handle_inbound  # noqa: F401 — re-exported
from .platform.client import PlatformClient
from .telegram.channel import TelegramChannel, diff_card as _diff_card, parse_update  # noqa: F401


async def handle_update(
    update: dict[str, Any], tg, api: PlatformClient, *, turn_timeout: float
) -> None:
    ctx = parse_update(update)
    if ctx is None:
        return
    await handle_inbound(ctx, TelegramChannel(tg), api, turn_timeout=turn_timeout)
