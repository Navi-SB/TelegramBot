"""A thin WhatsApp Cloud API client — the four calls this bot actually makes.

Plain HTTPS against graph.facebook.com; Meta archived its SDK in 2023 and raw
POSTs are the norm. Same philosophy as the Telegram client: no framework, one
retry ladder, errors mapped to what dispatch can act on.

The error codes that matter:
  130429 / 131056  throughput or business↔consumer pair rate limit → backoff+retry
  131026           undeliverable: the user blocked us or isn't on WhatsApp —
                   this is the ONLY blocked signal WhatsApp gives (there is no
                   my_chat_member equivalent), so the channel turns it into
                   mark_blocked
  131047           the 24h customer-service window closed — cannot happen to a
                   purely reactive bot except on Meta's extreme retry tail;
                   nothing useful can be sent without a paid template
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from ..logging import log_exception

GRAPH = "https://graph.facebook.com"
_RETRYABLE = {130429, 131056}


class WhatsAppError(RuntimeError):
    def __init__(self, code: int, subcode: Optional[int], description: str):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.subcode = subcode
        self.description = description


class WhatsAppUndeliverable(WhatsAppError):
    """131026 — blocked us, or the number isn't on WhatsApp."""


class WhatsAppWindowClosed(WhatsAppError):
    """131047 — outside the 24h customer-service window."""


class WhatsAppClient:
    def __init__(
        self, token: str, phone_number_id: str, client: httpx.AsyncClient,
        graph_version: str = "v26.0",
    ):
        self._url = f"{GRAPH}/{graph_version}/{phone_number_id}/messages"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._http = client

    async def _call(self, payload: dict[str, Any], *, retries: int = 2) -> Any:
        payload = {"messaging_product": "whatsapp", **payload}
        for attempt in range(retries + 1):
            try:
                r = await self._http.post(self._url, json=payload, headers=self._headers)
                body = r.json()
            except Exception as exc:  # transport
                if attempt == retries:
                    raise
                log_exception("whatsapp_transport_retry", exc)
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

            err = body.get("error")
            if not err and r.status_code < 400:
                return body

            code = int((err or {}).get("code", r.status_code))
            subcode = (err or {}).get("error_subcode")
            desc = (err or {}).get("message", "")
            if code in _RETRYABLE and attempt < retries:
                # No retry_after hint exists on the Cloud API; fixed backoff.
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            if r.status_code >= 500 and code >= 500 and attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if code == 131026:
                raise WhatsAppUndeliverable(code, subcode, desc)
            if code == 131047:
                raise WhatsAppWindowClosed(code, subcode, desc)
            raise WhatsAppError(code, subcode, desc)
        raise WhatsAppError(0, None, "unreachable")

    @staticmethod
    def _wamid(res: Any) -> Optional[str]:
        try:
            return res["messages"][0]["id"]
        except Exception:
            return None

    # --- the four calls -----------------------------------------------------

    async def send_text(self, to: str, body: str) -> Optional[str]:
        res = await self._call({
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": body, "preview_url": False},
        })
        return self._wamid(res)

    async def send_buttons(
        self, to: str, body: str, buttons: list[tuple[str, str]]
    ) -> Optional[str]:
        """Reply buttons: max 3, title ≤ 20 chars, body ≤ 1024."""
        res = await self._call({
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": bid, "title": title[:20]}}
                    for bid, title in buttons[:3]
                ]},
            },
        })
        return self._wamid(res)

    async def send_list(
        self, to: str, body: str, button_text: str,
        rows: list[dict[str, str]], section_title: str = "Options",
    ) -> Optional[str]:
        """List message: max 10 rows, row title ≤ 24, description ≤ 72."""
        res = await self._call({
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body},
                "action": {
                    "button": button_text[:20],
                    "sections": [{"title": section_title[:24], "rows": rows[:10]}],
                },
            },
        })
        return self._wamid(res)

    async def mark_read(self, message_id: str, *, typing: bool = False) -> None:
        """Read receipt, optionally with the typing indicator (~25s).
        Cosmetic — never worth failing a turn over."""
        payload: dict[str, Any] = {"status": "read", "message_id": message_id}
        if typing:
            payload["typing_indicator"] = {"type": "text"}
        try:
            await self._call(payload, retries=0)
        except Exception:
            pass
