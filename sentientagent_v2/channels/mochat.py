"""Mochat channel adapter (HTTP outbound + polling inbound)."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..bus.events import OutboundMessage
from .base import BaseChannel

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MochatTarget:
    """Resolved Mochat outbound target."""

    id: str
    is_panel: bool


class MochatChannel(BaseChannel):
    """Mochat adapter with HTTP outbound and polling inbound support."""

    name = "mochat"

    def __init__(
        self,
        bus,
        *,
        base_url: str,
        claw_token: str,
        agent_user_id: str = "",
        sessions: list[str] | None = None,
        panels: list[str] | None = None,
        allow_from: list[str] | None = None,
        poll_interval_seconds: int = 5,
        watch_timeout_ms: int = 15000,
        watch_limit: int = 20,
        panel_limit: int = 50,
        max_seen_message_ids: int = 2000,
    ) -> None:
        super().__init__(bus, allow_from=allow_from)
        self.base_url = base_url.strip().rstrip("/")
        self.claw_token = claw_token.strip()
        self.agent_user_id = agent_user_id.strip()
        self.sessions = tuple(
            dict.fromkeys(str(item).strip() for item in (sessions or []) if str(item).strip())
        )
        self.panels = tuple(
            dict.fromkeys(str(item).strip() for item in (panels or []) if str(item).strip())
        )
        self.poll_interval_seconds = max(int(poll_interval_seconds), 1)
        self.watch_timeout_ms = max(int(watch_timeout_ms), 1000)
        self.watch_limit = max(int(watch_limit), 1)
        self.panel_limit = max(int(panel_limit), 1)
        self.max_seen_message_ids = max(int(max_seen_message_ids), 100)

        self._poll_task: asyncio.Task[None] | None = None
        self._session_cursors: dict[str, int] = {sid: 0 for sid in self.sessions}
        self._seen_message_ids: set[str] = set()
        self._seen_order: deque[str] = deque()

    async def start(self) -> None:
        if not self.base_url:
            raise RuntimeError("Missing MOCHAT_BASE_URL for mochat channel.")
        if not self.claw_token:
            raise RuntimeError("Missing MOCHAT_CLAW_TOKEN for mochat channel.")
        self._running = True
        if not self.sessions and not self.panels:
            logger.info("Mochat inbound polling disabled: no sessions/panels configured.")
            return
        if self._poll_task and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_loop(), name="mochat-poll")

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def send(self, msg: OutboundMessage) -> None:
        content = (msg.content or "").strip()
        if not content:
            return

        target = self.resolve_target(msg.chat_id)
        if not target.id:
            logger.warning("Skip Mochat send: empty target chat_id.")
            return

        payload: dict[str, Any]
        if target.is_panel:
            payload = {"panelId": target.id, "content": content}
            group_id = self._read_group_id(msg.metadata)
            if group_id:
                payload["groupId"] = group_id
            if msg.reply_to:
                payload["replyTo"] = msg.reply_to
            await self._post_json("/api/claw/groups/panels/send", payload)
            return

        payload = {"sessionId": target.id, "content": content}
        if msg.reply_to:
            payload["replyTo"] = msg.reply_to
        await self._post_json("/api/claw/sessions/send", payload)

    @staticmethod
    def resolve_target(raw: str) -> MochatTarget:
        """Resolve target id/type from user chat_id string."""
        text = (raw or "").strip()
        if not text:
            return MochatTarget(id="", is_panel=False)

        lowered = text.lower()
        cleaned = text
        forced_panel = False
        for prefix in ("mochat:", "group:", "channel:", "panel:"):
            if lowered.startswith(prefix):
                cleaned = text[len(prefix):].strip()
                forced_panel = prefix in {"group:", "channel:", "panel:"}
                break
        if not cleaned:
            return MochatTarget(id="", is_panel=False)
        return MochatTarget(id=cleaned, is_panel=forced_panel or not cleaned.startswith("session_"))

    def _post_json_sync(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Claw-Token": self.claw_token,
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
        except HTTPError as exc:
            raise RuntimeError(f"Mochat API HTTP error ({path}): {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"Mochat API network error ({path}): {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Mochat API invalid JSON ({path}): {exc}") from exc

        if not isinstance(parsed, dict):
            return {}
        if isinstance(parsed.get("code"), int):
            code = int(parsed.get("code", 0))
            if code != 200:
                message = str(parsed.get("message", "request failed"))
                raise RuntimeError(f"Mochat API error ({path}): {message} (code={code})")
            data = parsed.get("data")
            return data if isinstance(data, dict) else {}
        return parsed

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        call = functools.partial(self._post_json_sync, path, payload)
        return await loop.run_in_executor(None, call)

    @staticmethod
    def _read_group_id(metadata: dict[str, Any]) -> str | None:
        if not isinstance(metadata, dict):
            return None
        value = metadata.get("group_id") or metadata.get("groupId")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Mochat polling iteration failed")
            await asyncio.sleep(self.poll_interval_seconds)

    async def _poll_once(self) -> None:
        for session_id in self.sessions:
            await self._poll_session(session_id)
        for panel_id in self.panels:
            await self._poll_panel(panel_id)

    async def _poll_session(self, session_id: str) -> None:
        payload = {
            "sessionId": session_id,
            "cursor": self._session_cursors.get(session_id, 0),
            "timeoutMs": self.watch_timeout_ms,
            "limit": self.watch_limit,
        }
        response = await self._post_json("/api/claw/sessions/watch", payload)
        self._update_session_cursor(session_id, response)
        for item in self._extract_session_messages(response):
            await self._publish_mochat_message(
                source="session",
                target_id=session_id,
                item=item,
                extra_metadata={},
            )

    async def _poll_panel(self, panel_id: str) -> None:
        response = await self._post_json(
            "/api/claw/groups/panels/messages",
            {"panelId": panel_id, "limit": self.panel_limit},
        )
        raw_messages = response.get("messages")
        if not isinstance(raw_messages, list):
            return
        group_id = str(response.get("groupId", "")).strip()
        for item in reversed(raw_messages):
            if not isinstance(item, dict):
                continue
            await self._publish_mochat_message(
                source="panel",
                target_id=panel_id,
                item=item,
                extra_metadata={"group_id": group_id} if group_id else {},
            )

    def _extract_session_messages(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(response, dict):
            return []

        messages: list[dict[str, Any]] = []
        raw_messages = response.get("messages")
        if isinstance(raw_messages, list):
            messages.extend(item for item in raw_messages if isinstance(item, dict))

        raw_events = response.get("events")
        if isinstance(raw_events, list):
            for event in raw_events:
                if not isinstance(event, dict):
                    continue
                payload = event.get("payload")
                if isinstance(payload, dict):
                    messages.append(payload)

        raw_payload = response.get("payload")
        if isinstance(raw_payload, dict):
            messages.append(raw_payload)

        return messages

    def _update_session_cursor(self, session_id: str, response: dict[str, Any]) -> None:
        if not isinstance(response, dict):
            return
        current = self._session_cursors.get(session_id, 0)
        cursor_candidates = [
            response.get("cursor"),
            response.get("nextCursor"),
            response.get("latestCursor"),
        ]
        for value in cursor_candidates:
            try:
                cursor = int(value)
            except Exception:
                continue
            if cursor >= current:
                self._session_cursors[session_id] = cursor
                return

    async def _publish_mochat_message(
        self,
        *,
        source: str,
        target_id: str,
        item: dict[str, Any],
        extra_metadata: dict[str, Any],
    ) -> None:
        message_id = str(item.get("messageId") or item.get("id") or "").strip()
        dedup_key = f"{source}:{target_id}:{message_id}" if message_id else ""
        if dedup_key and self._is_seen(dedup_key):
            return

        sender_id = self._extract_sender_id(item)
        if not sender_id:
            return
        if self.agent_user_id and sender_id == self.agent_user_id:
            return

        content = self._normalize_content(item.get("content"))
        if not content:
            return

        chat_id = target_id if source == "session" else f"panel:{target_id}"
        metadata = {
            "message_id": message_id,
            "source": source,
            **extra_metadata,
        }
        await self.publish_inbound(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            metadata=metadata,
        )
        if dedup_key:
            self._mark_seen(dedup_key)

    @staticmethod
    def _extract_sender_id(item: dict[str, Any]) -> str:
        sender = item.get("author") or item.get("senderId") or item.get("sender_id") or ""
        sender_id = str(sender).strip()
        if sender_id:
            return sender_id
        author_info = item.get("authorInfo")
        if not isinstance(author_info, dict):
            return ""
        for key in ("id", "userId", "_id"):
            value = author_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _normalize_content(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if content is None:
            return ""
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content).strip()

    def _is_seen(self, key: str) -> bool:
        return key in self._seen_message_ids

    def _mark_seen(self, key: str) -> None:
        self._seen_message_ids.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > self.max_seen_message_ids:
            old = self._seen_order.popleft()
            self._seen_message_ids.discard(old)
