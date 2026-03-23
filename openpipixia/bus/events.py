"""Event models for bus <-> channel <-> gateway communication."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class InboundMessage:
    """Message received from a channel and sent to the gateway."""

    channel: str
    sender_id: str
    chat_id: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        return f"{self.channel}:{self.chat_id}"


@dataclass(slots=True)
class OutboundMessage:
    """Message produced by the gateway and sent to a channel."""

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
