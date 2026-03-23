"""Base channel interface."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from ..bus.events import InboundMessage, OutboundMessage
from ..bus.queue import MessageBus

logger = logging.getLogger(__name__)


class BaseChannel(ABC):
    """Base interface for all channel adapters."""

    name: str = "base"

    def __init__(self, bus: MessageBus, allow_from: list[str] | None = None):
        self.bus = bus
        self._running = False
        self._allow_from = tuple(str(item).strip() for item in (allow_from or []) if str(item).strip())

    @abstractmethod
    async def start(self) -> None:
        """Start channel workers/connections."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop channel workers/connections."""

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """Send outbound message to channel."""

    async def publish_inbound(
        self,
        *,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.is_allowed(sender_id):
            logger.warning(
                "Access denied for sender %s on channel %s (allow_from configured).",
                sender_id,
                self.name,
            )
            return
        await self.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=media or [],
                metadata=metadata or {},
            )
        )

    def is_allowed(self, sender_id: str) -> bool:
        """Return whether the sender is permitted by allowlist policy."""
        if not self._allow_from:
            return True
        sender = str(sender_id)
        if sender in self._allow_from:
            return True
        # Support composite ids such as "primary|secondary".
        if "|" in sender:
            return any(part and part in self._allow_from for part in sender.split("|"))
        return False

    @property
    def is_running(self) -> bool:
        return self._running
