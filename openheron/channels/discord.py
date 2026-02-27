"""Discord channel adapter (REST polling inbound + REST outbound)."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..bus.events import OutboundMessage
from .base import BaseChannel
from .polling_utils import cancel_background_task, dedupe_stripped, parse_json_payload, run_poll_loop

logger = logging.getLogger(__name__)


class DiscordChannel(BaseChannel):
    """Minimal Discord adapter using the public REST API."""

    name = "discord"

    def __init__(
        self,
        bus,
        *,
        token: str,
        allow_from: list[str] | None = None,
        poll_channels: list[str] | None = None,
        poll_interval_seconds: int = 10,
        include_bots: bool = False,
        api_base: str = "https://discord.com/api/v10",
    ) -> None:
        super().__init__(bus, allow_from=allow_from)
        self.token = token.strip()
        self.poll_channels = dedupe_stripped(poll_channels)
        self.poll_interval_seconds = max(int(poll_interval_seconds), 3)
        self.include_bots = bool(include_bots)
        self.api_base = api_base.rstrip("/")

        self._poll_task: asyncio.Task[None] | None = None
        self._latest_message_id: dict[str, str] = {}

    def _endpoint(self, path: str, query: dict[str, str] | None = None) -> str:
        if query:
            return f"{self.api_base}{path}?{urlencode(query)}"
        return f"{self.api_base}{path}"

    def _api_call_sync(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> Any:
        """Perform one blocking Discord REST call."""
        if not self.token:
            raise RuntimeError("Missing DISCORD_BOT_TOKEN for discord channel.")

        body: bytes | None = None
        headers = {
            "Authorization": f"Bot {self.token}",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        req = Request(
            self._endpoint(path, query),
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(req, timeout=max(self.poll_interval_seconds + 10, 15)) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"Discord API HTTP error ({path}): {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"Discord API network error ({path}): {exc.reason}") from exc

        return parse_json_payload(raw, error_context=f"Discord API invalid JSON ({path})")

    async def _api_call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> Any:
        loop = asyncio.get_running_loop()
        call = functools.partial(
            self._api_call_sync,
            method,
            path,
            payload=payload,
            query=query,
        )
        return await loop.run_in_executor(None, call)

    async def start(self) -> None:
        if not self.token:
            raise RuntimeError("Missing DISCORD_BOT_TOKEN for discord channel.")
        if self._poll_task and not self._poll_task.done():
            return

        self._running = True
        if self.poll_channels:
            self._poll_task = asyncio.create_task(self._poll_loop(), name="discord-poll")

    async def stop(self) -> None:
        self._running = False
        await cancel_background_task(self._poll_task)
        self._poll_task = None

    async def send(self, msg: OutboundMessage) -> None:
        channel_id = msg.chat_id.strip()
        if not channel_id:
            logger.warning("Skip Discord send: empty chat_id.")
            return
        await self._api_call(
            "POST",
            f"/channels/{channel_id}/messages",
            payload={"content": msg.content or "[empty message]"},
            query=None,
        )

    async def _poll_loop(self) -> None:
        await run_poll_loop(
            is_running=lambda: self._running,
            poll_once=self._poll_once,
            interval_seconds=self.poll_interval_seconds,
            logger=logger,
            error_message="Discord polling iteration failed",
            retry_delay_seconds=2,
        )

    async def _poll_once(self) -> None:
        """Poll configured Discord channels for new messages."""
        for channel_id in self.poll_channels:
            previous = self._latest_message_id.get(channel_id, "")
            query = {"limit": "50"}
            if previous:
                query["after"] = previous
            raw_messages = await self._api_call(
                "GET",
                f"/channels/{channel_id}/messages",
                payload=None,
                query=query,
            )
            if not isinstance(raw_messages, list):
                continue

            messages = [item for item in raw_messages if isinstance(item, dict)]
            messages.sort(key=lambda item: self._id_value(str(item.get("id", ""))))

            newest = previous
            for item in messages:
                message_id = str(item.get("id", "")).strip()
                if self._is_newer_id(message_id, newest):
                    newest = message_id

                normalized = self._normalize_inbound(item)
                if normalized is None:
                    continue
                sender_id, content, metadata = normalized
                await self.publish_inbound(
                    sender_id=sender_id,
                    chat_id=channel_id,
                    content=content,
                    metadata={
                        "channel_id": channel_id,
                        "chat_type": "channel",
                        "peer_kind": "channel",
                        "peer_id": channel_id,
                        "peer": {"kind": "channel", "id": channel_id},
                        "message_id": message_id,
                        **metadata,
                    },
                )

            if self._is_newer_id(newest, previous):
                self._latest_message_id[channel_id] = newest

    def _normalize_inbound(self, item: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
        author = item.get("author")
        if not isinstance(author, dict):
            return None

        sender_id = str(author.get("id", "")).strip()
        if not sender_id:
            return None
        if not self.include_bots and bool(author.get("bot")):
            return None

        content = str(item.get("content", "")).strip()
        if not content:
            return None

        metadata: dict[str, Any] = {
            "username": str(author.get("username", "")).strip(),
        }
        guild_id = str(item.get("guild_id") or item.get("guildId") or "").strip()
        if guild_id:
            metadata["guild_id"] = guild_id
            metadata["guildId"] = guild_id
            metadata["guild"] = {"id": guild_id}

        team_id = str(item.get("team_id") or item.get("teamId") or "").strip()
        if team_id:
            metadata["team_id"] = team_id
            metadata["teamId"] = team_id
            metadata["team"] = {"id": team_id}

        member = item.get("member") if isinstance(item.get("member"), dict) else {}
        raw_roles = member.get("roles")
        if isinstance(raw_roles, list):
            roles = [str(role).strip() for role in raw_roles if str(role).strip()]
            if roles:
                metadata["roles"] = roles
                metadata["role_ids"] = roles
        return sender_id, content, metadata

    @staticmethod
    def _id_value(value: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _is_newer_id(cls, candidate: str, baseline: str) -> bool:
        return cls._id_value(candidate) > cls._id_value(baseline)
