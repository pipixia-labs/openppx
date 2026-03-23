"""Telegram channel adapter (long-polling inbound + Bot API outbound)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import BaseChannel
from .polling_utils import cancel_background_task, parse_json_payload, run_poll_loop
from ..bus.events import OutboundMessage

logger = logging.getLogger(__name__)


class TelegramChannel(BaseChannel):
    """Minimal Telegram adapter using Bot API long polling."""

    name = "telegram"

    def __init__(
        self,
        bus,
        *,
        token: str,
        allow_from: list[str] | None = None,
        proxy: str = "",
        api_base: str = "https://api.telegram.org",
        poll_timeout_seconds: int = 20,
    ) -> None:
        super().__init__(bus, allow_from=allow_from)
        self.token = token.strip()
        self.proxy = proxy.strip()
        self.api_base = api_base.rstrip("/")
        self.poll_timeout_seconds = max(int(poll_timeout_seconds), 1)
        self._poll_task: asyncio.Task[None] | None = None
        self._offset: int = 0

    def _endpoint(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    def _api_call_sync(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Perform one blocking Telegram Bot API call."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            self._endpoint(method),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.poll_timeout_seconds + 10) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"Telegram API HTTP error ({method}): {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"Telegram API network error ({method}): {exc.reason}") from exc

        parsed = parse_json_payload(raw, error_context=f"Telegram API invalid JSON ({method})")

        if not isinstance(parsed, dict) or not parsed.get("ok", False):
            message = ""
            if isinstance(parsed, dict):
                message = str(parsed.get("description", "unknown error"))
            raise RuntimeError(f"Telegram API failed ({method}): {message}")
        result = parsed.get("result")
        return result if isinstance(result, dict) else {"result": result}

    async def _api_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._api_call_sync, method, payload)

    async def start(self) -> None:
        if not self.token:
            raise RuntimeError("Missing TELEGRAM_BOT_TOKEN for telegram channel.")
        if self._poll_task and not self._poll_task.done():
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name="telegram-poll")

    async def stop(self) -> None:
        self._running = False
        await cancel_background_task(self._poll_task)
        self._poll_task = None

    async def send(self, msg: OutboundMessage) -> None:
        text = msg.content if msg.content else "[empty message]"
        await self._api_call(
            "sendMessage",
            {
                "chat_id": msg.chat_id,
                "text": text,
            },
        )

    async def _poll_loop(self) -> None:
        await run_poll_loop(
            is_running=lambda: self._running,
            poll_once=self._poll_once,
            interval_seconds=0,
            logger=logger,
            error_message="Telegram polling iteration failed",
            retry_delay_seconds=2,
        )

    async def _poll_once(self) -> None:
        """Run one Telegram getUpdates cycle and publish normalized updates."""
        response = await self._api_call(
            "getUpdates",
            {
                "offset": self._offset,
                "timeout": self.poll_timeout_seconds,
                "allowed_updates": ["message"],
            },
        )
        updates = response.get("result")
        if not isinstance(updates, list):
            return
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = max(self._offset, update_id + 1)
            await self._process_update(update)

    async def _process_update(self, update: dict[str, Any]) -> None:
        """Normalize one Telegram update into bus inbound format."""
        message = update.get("message")
        if not isinstance(message, dict):
            return

        sender = message.get("from", {})
        if not isinstance(sender, dict):
            sender = {}
        sender_id_raw = sender.get("id")
        if sender_id_raw is None:
            return
        sender_id = str(sender_id_raw)
        username = str(sender.get("username", "")).strip()
        sender_identity = f"{sender_id}|@{username}" if username else sender_id

        chat = message.get("chat", {})
        if not isinstance(chat, dict):
            chat = {}
        chat_id_raw = chat.get("id")
        if chat_id_raw is None:
            return
        chat_id = str(chat_id_raw)

        text = str(message.get("text", "")).strip()
        if not text:
            text = str(message.get("caption", "")).strip()
        if not text:
            return

        metadata = {
            "message_id": str(message.get("message_id", "")),
            "chat_type": str(chat.get("type", "")),
            "update_id": str(update.get("update_id", "")),
        }
        await self.publish_inbound(
            sender_id=sender_identity,
            chat_id=chat_id,
            content=text,
            metadata=metadata,
        )
