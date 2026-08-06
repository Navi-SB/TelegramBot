"""The WhatsApp worker loop, kept importable and testable.

Two failure modes this exists to prevent (both found in review):

1. A PlatformError (not just unreachability) on one message's claim must not
   abort the rest of the batch — the webhook already 200'd, so Meta will
   never redeliver, and aborted messages would vanish silently.

2. One invocation runs its envelopes sequentially inside a single Vercel
   maxDuration budget. N long turns × deadline_seconds can exceed it, and a
   SIGKILL mid-batch loses everything in flight with zero logs. Each turn
   gets only what remains of the budget, and when almost nothing remains the
   leftovers are dropped LOUDLY instead of silently.
"""
from __future__ import annotations

import time
from typing import Callable

from ..core.dispatch import Channel, Inbound, handle_inbound
from ..logging import log, log_exception
from ..platform.client import PlatformClient, PlatformError
from .render import log_ref

# Stop taking new turns when less than this much budget remains — enough to
# finish logging and return 200 cleanly.
_MIN_TURN_SECONDS = 30.0
_BUDGET_HEADROOM = 20.0


async def process_envelopes(
    envelopes: list[Inbound],
    ch: Channel,
    api: PlatformClient,
    *,
    turn_timeout: float,
    budget_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    started = clock()
    for i, ctx in enumerate(envelopes):
        remaining = budget_seconds - _BUDGET_HEADROOM - (clock() - started)
        if remaining < _MIN_TURN_SECONDS:
            # The webhook already acked, so these are gone — say so loudly.
            log("wa_batch_budget_exhausted", chat_id=ctx.log_ref,
                chunks=f"{i}/{len(envelopes)}")
            return

        # Dedupe BEFORE doing anything with side effects. Any platform error
        # here skips THIS message only; the rest of the batch still runs.
        try:
            if not await api.claim_event(ctx.event_id):
                log("duplicate_update", chat_id=ctx.log_ref,
                    update_id=log_ref(ctx.event_id))
                continue
        except PlatformError as exc:
            log_exception("claim_failed", exc, chat_id=ctx.log_ref,
                          update_id=log_ref(ctx.event_id))
            continue

        try:
            await handle_inbound(ctx, ch, api,
                                 turn_timeout=min(turn_timeout, remaining))
        except Exception as exc:  # noqa: BLE001 — outermost per-message boundary
            log_exception("worker_failed", exc, chat_id=ctx.log_ref,
                          update_id=log_ref(ctx.event_id))
