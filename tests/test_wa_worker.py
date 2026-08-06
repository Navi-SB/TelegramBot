"""The worker loop's batch resilience — findings from the branch review.

The webhook has already 200'd by the time these run, so Meta never redelivers:
anything the loop drops is gone. It must therefore never let one message's
failure, or the function's own time budget, take the rest down silently.
"""
import pytest

from bot.platform.client import PlatformError
from bot.whatsapp.channel import WhatsAppChannel
from bot.whatsapp.parse import parse_envelopes
from bot.whatsapp.worker import process_envelopes
from tests.test_dispatch import FakeApi
from tests.test_wa_parse import text_msg, wa_body
from tests.test_whatsapp_dispatch import FakeWa


def envelopes(n=3):
    return parse_envelopes(wa_body([text_msg(f"m{i}", mid=f"wamid.{i}") for i in range(n)]))


class ClaimApi(FakeApi):
    """claim_event answers per-wamid: True / False (duplicate) / raise."""

    def __init__(self, plan, **kw):
        super().__init__(**kw)
        self._plan = plan
        self.claimed = []

    async def claim_event(self, event_id):
        self.claimed.append(event_id)
        action = self._plan.get(event_id, True)
        if isinstance(action, Exception):
            raise action
        return action


@pytest.mark.asyncio
async def test_a_platform_error_on_one_claim_does_not_abort_the_batch():
    """A 500/401 from the platform is a PlatformError, not PlatformUnavailable —
    it used to escape the guard and silently drop every later message."""
    api = ClaimApi({"wamid.1": PlatformError(503, "upstream hiccup")})
    wa = FakeWa()
    await process_envelopes(envelopes(3), WhatsAppChannel(wa, api), api,
                            turn_timeout=30, budget_seconds=290)
    assert api.claimed == ["wamid.0", "wamid.1", "wamid.2"]  # all three tried
    assert api.calls.count("turn") == 2                      # 0 and 2 answered


@pytest.mark.asyncio
async def test_a_duplicate_claim_skips_only_that_message():
    api = ClaimApi({"wamid.0": False})
    wa = FakeWa()
    await process_envelopes(envelopes(2), WhatsAppChannel(wa, api), api,
                            turn_timeout=30, budget_seconds=290)
    assert api.calls.count("turn") == 1


@pytest.mark.asyncio
async def test_an_exhausted_time_budget_stops_loudly_instead_of_being_sigkilled():
    """N × deadline can exceed the function's maxDuration; the loop must stop
    taking turns before Vercel kills it mid-send."""
    api = ClaimApi({})
    wa = FakeWa()
    t = [0.0]

    def clock():
        t[0] += 150.0  # every step of the loop costs 150 "seconds"
        return t[0]

    await process_envelopes(envelopes(3), WhatsAppChannel(wa, api), api,
                            turn_timeout=265, budget_seconds=290, clock=clock)
    # First turn ran; by the second envelope the budget was spent — no claim,
    # no half-processed turn for Vercel to SIGKILL.
    assert api.calls.count("turn") == 1
    assert len(api.claimed) == 1


@pytest.mark.asyncio
async def test_a_turn_never_gets_more_timeout_than_the_remaining_budget():
    seen = []

    class TimeoutApi(ClaimApi):
        async def turn(self, chat_id, content, *, timeout):
            seen.append(timeout)
            return await super().turn(chat_id, content, timeout=timeout)

    api = TimeoutApi({})
    t = [0.0]

    def clock():
        t[0] += 40.0
        return t[0]

    await process_envelopes(envelopes(3), WhatsAppChannel(FakeWa(), api), api,
                            turn_timeout=265, budget_seconds=290, clock=clock)
    assert seen == sorted(seen, reverse=True)   # later turns get less
    assert all(s <= 265 for s in seen)
