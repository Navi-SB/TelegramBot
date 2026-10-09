# P1 outbound delivery contract

Companion platform branch: `Navi-SB/VoyageCalc`,
`alexandros-kv/p1-fixtures-delivery-integrity`.

Deploy this bridge change before enabling the platform's durable recovery controls.

- `ok: true` acknowledges every sent chunk. A previously claimed key alone returns
  `duplicate_unknown` with `certain: false`.
- `certain: true` on a refusal certifies that no chunk was sent. A first-chunk 429
  returns `rate_limited`; a refusal after any successful chunk stays uncertain.
- The platform persists retry backoff and gives each definitively safe retry a new
  attempt key. This endpoint requires a key and does not release claimed keys.
- Push sends do not perform hidden retries. General Telegram message sends also
  avoid resending after transport errors or 5xx responses; definite rate-limit
  responses retain their existing bounded retries for interactive replies.
- Oversized messages are rejected before sending. They are never truncated silently.

Tests use fake platform/Telegram clients or httpx MockTransport; no live chats are
messaged by the suite. Verify a real scheduled report in an authorized test chat after
both services are deployed and their existing push secrets/configuration are installed.
