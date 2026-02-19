"""Slack channel adapter (Web API polling inbound + chat.postMessage outbound)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..bus.events import OutboundMessage
from .base import BaseChannel
from .polling_utils import cancel_background_task, dedupe_stripped, parse_json_payload, run_poll_loop

logger = logging.getLogger(__name__)


class SlackChannel(BaseChannel):
    """Minimal Slack adapter built on the Slack Web API."""

    name = "slack"

    def __init__(
        self,
        bus,
        *,
        bot_token: str,
        app_token: str = "",
        default_channel: str = "",
        allow_from: list[str] | None = None,
        poll_channels: list[str] | None = None,
        poll_interval_seconds: int = 15,
        include_bots: bool = False,
        api_base: str = "https://slack.com/api",
    ) -> None:
        super().__init__(bus, allow_from=allow_from)
        self.bot_token = bot_token.strip()
        self.app_token = app_token.strip()
        self.default_channel = default_channel.strip()
        self.poll_channels = dedupe_stripped(poll_channels)
        self.poll_interval_seconds = max(int(poll_interval_seconds), 5)
        self.include_bots = bool(include_bots)
        self.api_base = api_base.rstrip("/")

        self._poll_task: asyncio.Task[None] | None = None
        self._latest_ts: dict[str, str] = {}

    def _endpoint(self, method: str) -> str:
        return f"{self.api_base}/{method}"

    def _api_call_sync(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Perform one blocking Slack Web API call."""
        if not self.bot_token:
            raise RuntimeError("Missing SLACK_BOT_TOKEN for slack channel.")

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            self._endpoint(method),
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.bot_token}",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=max(self.poll_interval_seconds + 10, 15)) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"Slack API HTTP error ({method}): {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"Slack API network error ({method}): {exc.reason}") from exc

        parsed = parse_json_payload(raw, error_context=f"Slack API invalid JSON ({method})")

        if not isinstance(parsed, dict) or not parsed.get("ok", False):
            error_text = ""
            if isinstance(parsed, dict):
                error_text = str(parsed.get("error", "unknown_error"))
            raise RuntimeError(f"Slack API failed ({method}): {error_text}")
        return parsed

    async def _api_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._api_call_sync, method, payload)

    async def start(self) -> None:
        if not self.bot_token:
            raise RuntimeError("Missing SLACK_BOT_TOKEN for slack channel.")
        if self._poll_task and not self._poll_task.done():
            return

        self._running = True
        if self.poll_channels:
            self._poll_task = asyncio.create_task(self._poll_loop(), name="slack-poll")

    async def stop(self) -> None:
        self._running = False
        await cancel_background_task(self._poll_task)
        self._poll_task = None

    async def send(self, msg: OutboundMessage) -> None:
        target_channel = msg.chat_id.strip() or self.default_channel
        if not target_channel:
            logger.warning("Skip Slack send: empty chat_id and no default channel configured.")
            return
        payload: dict[str, Any] = {
            "channel": target_channel,
            "text": msg.content or "[empty message]",
        }
        if isinstance(msg.metadata, dict):
            thread_ts = str(msg.metadata.get("thread_ts", "")).strip()
            if thread_ts:
                payload["thread_ts"] = thread_ts
        await self._api_call("chat.postMessage", payload)

    async def _poll_loop(self) -> None:
        await run_poll_loop(
            is_running=lambda: self._running,
            poll_once=self._poll_once,
            interval_seconds=self.poll_interval_seconds,
            logger=logger,
            error_message="Slack polling iteration failed",
            retry_delay_seconds=2,
        )

    async def _poll_once(self) -> None:
        """Poll configured Slack channels for new user messages."""
        for channel_id in self.poll_channels:
            previous = self._latest_ts.get(channel_id, "")
            payload: dict[str, Any] = {
                "channel": channel_id,
                "limit": 20,
                "inclusive": False,
            }
            if previous:
                payload["oldest"] = previous

            response = await self._api_call("conversations.history", payload)
            raw_messages = response.get("messages")
            if not isinstance(raw_messages, list):
                continue

            messages = [item for item in raw_messages if isinstance(item, dict)]
            messages.sort(key=lambda item: self._ts_value(str(item.get("ts", ""))))

            newest = previous
            for item in messages:
                ts = str(item.get("ts", "")).strip()
                if self._is_newer_ts(ts, newest):
                    newest = ts
                if not self._is_user_message(item):
                    continue

                sender_id = str(item.get("user", "")).strip()
                text = str(item.get("text", "")).strip()
                await self.publish_inbound(
                    sender_id=sender_id,
                    chat_id=channel_id,
                    content=text,
                    metadata={
                        "channel_id": channel_id,
                        "ts": ts,
                        "thread_ts": str(item.get("thread_ts", "")).strip(),
                    },
                )

            if self._is_newer_ts(newest, previous):
                self._latest_ts[channel_id] = newest

    def _is_user_message(self, item: dict[str, Any]) -> bool:
        if str(item.get("type", "")).strip() != "message":
            return False
        if item.get("subtype"):
            return False
        if not self.include_bots and item.get("bot_id"):
            return False
        sender_id = str(item.get("user", "")).strip()
        content = str(item.get("text", "")).strip()
        return bool(sender_id and content)

    @staticmethod
    def _ts_value(value: str) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _is_newer_ts(cls, candidate: str, baseline: str) -> bool:
        return cls._ts_value(candidate) > cls._ts_value(baseline)
