"""A thin Telegram Bot API client — the ten methods this bot actually uses,
plus the one file download that isn't a method."""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from ..core.attachments import read_capped
from ..logging import log, log_exception


class TelegramError(RuntimeError):
    def __init__(self, code: int, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description


class TelegramClient:
    def __init__(self, token: str, client: httpx.AsyncClient):
        self._base = f"https://api.telegram.org/bot{token}"
        # Downloads live under a different path that ALSO embeds the token,
        # so neither URL may ever reach a log line or an exception message.
        self._file_base = f"https://api.telegram.org/file/bot{token}"
        self._http = client

    async def _call(self, method: str, *, retries: int = 2, **params) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        for attempt in range(retries + 1):
            try:
                r = await self._http.post(f"{self._base}/{method}", json=payload)
                body = r.json()
            except Exception as exc:  # transport
                if attempt == retries:
                    raise
                log_exception("telegram_transport_retry", exc)
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

            if body.get("ok"):
                return body.get("result")

            code = body.get("error_code", r.status_code)
            desc = body.get("description", "")
            if code == 429:
                # Telegram tells us exactly how long to wait; guessing would
                # either waste the deadline or get us limited harder.
                wait = float((body.get("parameters") or {}).get("retry_after", 1))
                if attempt < retries:
                    await asyncio.sleep(wait + 0.25)
                    continue
            if code >= 500 and attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise TelegramError(code, desc)
        raise TelegramError(0, "unreachable")

    # --- the ten methods ----------------------------------------------------

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        parse_mode: Optional[str] = "HTML",
        reply_markup: Optional[dict] = None,
    ) -> int:
        res = await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            link_preview_options={"is_disabled": True},
        )
        return res["message_id"]

    async def send_safe(
        self, chat_id: str | int, html: str, *, reply_markup: Optional[dict] = None
    ) -> Optional[int]:
        """Send as HTML; on a parse failure resend the same content as plain
        text with tags stripped.

        A formatting bug must NEVER cost the user their answer. Telegram
        rejects the WHOLE message on one bad entity, and agent output is full
        of the characters that cause it.
        """
        try:
            return await self.send_message(chat_id, html, reply_markup=reply_markup)
        except TelegramError as exc:
            if "can't parse entities" not in exc.description.lower():
                raise
            from .html import strip_tags

            log("telegram_html_fallback", status=exc.code)
            return await self.send_message(
                chat_id, strip_tags(html), parse_mode=None, reply_markup=reply_markup
            )

    async def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        *,
        parse_mode: Optional[str] = "HTML",
        reply_markup: Optional[dict] = None,
    ) -> None:
        try:
            await self._call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                link_preview_options={"is_disabled": True},
            )
        except TelegramError as exc:
            # Editing to identical content is not an error worth surfacing.
            if "message is not modified" in exc.description.lower():
                return
            if "can't parse entities" in exc.description.lower():
                from .html import strip_tags

                await self._call(
                    "editMessageText", chat_id=chat_id, message_id=message_id,
                    text=strip_tags(text), reply_markup=reply_markup,
                )
                return
            raise

    async def edit_reply_markup(
        self, chat_id: str | int, message_id: int, reply_markup: Optional[dict] = None
    ) -> None:
        try:
            await self._call(
                "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            if "message is not modified" not in exc.description.lower():
                raise

    async def answer_callback(
        self, callback_id: str, text: Optional[str] = None, *, alert: bool = False
    ) -> None:
        await self._call(
            "answerCallbackQuery", callback_query_id=callback_id, text=text,
            show_alert=alert, retries=0,
        )

    async def send_chat_action(self, chat_id: str | int, action: str = "typing") -> None:
        try:
            await self._call("sendChatAction", chat_id=chat_id, action=action, retries=0)
        except Exception:
            pass  # cosmetic; never worth failing a turn over

    async def set_webhook(self, url: str, secret: str, allowed: list[str]) -> Any:
        return await self._call(
            "setWebhook", url=url, secret_token=secret, allowed_updates=allowed,
            max_connections=10, drop_pending_updates=True,
        )

    async def delete_webhook(self) -> Any:
        return await self._call("deleteWebhook", drop_pending_updates=False)

    async def set_my_commands(self, commands: list[dict]) -> Any:
        return await self._call("setMyCommands", commands=commands)

    async def get_me(self) -> Any:
        return await self._call("getMe")

    async def get_file(self, file_id: str) -> dict[str, Any]:
        """{file_id, file_size?, file_path?} for a file someone sent. The
        cloud Bot API refuses files over 20 MB here with "file is too big"."""
        return await self._call("getFile", file_id=file_id) or {}

    # --- the download -------------------------------------------------------

    async def download_file(self, file_path: str, max_bytes: int) -> bytes:
        """Stream a getFile path, stopping past max_bytes. Raises only the
        typed attachment errors, whose messages never contain the URL."""
        return await read_capped(self._http, f"{self._file_base}/{file_path}", max_bytes)
