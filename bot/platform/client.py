"""Client for VoyageCalc's /api/bot/* surface.

Note what is NOT here: any way to name a user. Every call carries
(channel, chat_id) and the platform resolves that through its link table. That
is deliberate — it means this token, if it leaks, can only act for chats that
have already paired, rather than impersonating anyone by id.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

CHANNEL = "telegram"  # default; WhatsApp passes channel="whatsapp"


class PlatformError(RuntimeError):
    def __init__(self, status: int, detail: Any = None):
        super().__init__(f"platform {status}")
        self.status = status
        self.detail = detail


class PlatformUnavailable(PlatformError):
    """The backend could not be reached at all — distinct from it refusing."""


class PlatformClient:
    def __init__(
        self, base: str, service_token: str, client: httpx.AsyncClient,
        channel: str = CHANNEL,
    ):
        self._base = base.rstrip("/")
        self._http = client
        self._channel = channel
        self._headers = {"X-Service-Token": service_token, "X-Bot-Channel": channel}

    async def _call(self, method: str, path: str, **kw) -> Any:
        try:
            r = await self._http.request(
                method, f"{self._base}{path}", headers=self._headers, **kw
            )
        except Exception as exc:
            raise PlatformUnavailable(0, type(exc).__name__) from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = r.text[:120]
            raise PlatformError(r.status_code, detail)
        return r.json() if r.content else {}

    # --- dedupe -------------------------------------------------------------

    async def claim_event(self, event_id: str) -> bool:
        """False means this update has already been handled. Telegram retries
        for up to 24h, and a redelivered Approve would apply a write twice."""
        res = await self._call(
            "POST", "/api/bot/events/claim",
            json={"channel": self._channel, "event_id": str(event_id)},
        )
        return bool(res.get("fresh"))

    # --- linking ------------------------------------------------------------

    async def redeem(self, chat_id: str, code: str, tg_user_id: str, name: str | None):
        return await self._call("POST", "/api/bot/link/redeem", json={
            "channel": self._channel, "chat_id": chat_id, "code": code,
            "channel_user_id": tg_user_id, "display_name": name,
        })

    async def get_link(self, chat_id: str) -> dict[str, Any]:
        return await self._call(
            "GET", "/api/bot/link", params={"channel": self._channel, "chat_id": chat_id}
        )

    async def revoke(self, chat_id: str):
        return await self._call(
            "POST", "/api/bot/link/revoke", json={"channel": self._channel, "chat_id": chat_id}
        )

    async def mark_blocked(self, chat_id: str):
        return await self._call(
            "POST", "/api/bot/link/blocked", json={"channel": self._channel, "chat_id": chat_id}
        )

    # --- agents and sessions ------------------------------------------------

    async def agents(self, chat_id: str) -> dict[str, Any]:
        return await self._call(
            "GET", "/api/bot/agents", params={"channel": self._channel, "chat_id": chat_id}
        )

    async def set_session(
        self, chat_id: str, preset_id: Optional[str] = None, *, new_thread: bool = False
    ) -> dict[str, Any]:
        return await self._call("POST", "/api/bot/session", json={
            "channel": self._channel, "chat_id": chat_id,
            "preset_id": preset_id, "new_thread": new_thread,
        })

    # --- the turn -----------------------------------------------------------

    async def turn(self, chat_id: str, content: str, *, timeout: float) -> dict[str, Any]:
        """Run one agent turn. Long by nature — the platform assembles the
        reply, so the narration rule lives in exactly one place."""
        return await self._call(
            "POST", "/api/bot/turn",
            json={"channel": self._channel, "chat_id": chat_id, "content": content},
            timeout=timeout,
        )

    async def confirm(self, chat_id: str, pending_id: str, action: str) -> dict[str, Any]:
        return await self._call("POST", "/api/bot/confirm", json={
            "channel": self._channel, "chat_id": chat_id,
            "pending_id": pending_id, "action": action,
        })
