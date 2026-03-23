"""Local stdio channel for minimal gateway testing."""

from __future__ import annotations

import json
from typing import Callable

from ..bus.events import OutboundMessage
from ..bus.queue import MessageBus
from .base import BaseChannel


class LocalChannel(BaseChannel):
    """A local channel that prints outbound messages to stdout."""

    name = "local"

    def __init__(self, bus: MessageBus, writer: Callable[[str], None] | None = None):
        super().__init__(bus)
        self._writer = writer or print

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        payload = {
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "content": msg.content,
            "reply_to": msg.reply_to,
            "metadata": msg.metadata,
        }
        self._writer(json.dumps(payload, ensure_ascii=False))

    async def ingest_text(
        self,
        text: str,
        *,
        chat_id: str = "terminal",
        sender_id: str = "local-user",
    ) -> None:
        await self.publish_inbound(sender_id=sender_id, chat_id=chat_id, content=text)
