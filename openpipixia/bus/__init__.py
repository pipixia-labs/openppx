"""Message bus primitives for channel/gateway integration."""

from .events import InboundMessage, OutboundMessage
from .queue import MessageBus

__all__ = ["InboundMessage", "MessageBus", "OutboundMessage"]
