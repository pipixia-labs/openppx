"""Channel manager for outbound routing."""

from __future__ import annotations

import asyncio
import logging

from ..bus.queue import MessageBus
from ..runtime.step_events import classify_outbound_message
from .base import BaseChannel
from .polling_utils import cancel_background_task

logger = logging.getLogger(__name__)


class ChannelManager:
    """Manages channel lifecycle and outbound dispatching."""

    def __init__(self, bus: MessageBus):
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task[None] | None = None

    def register(self, channel: BaseChannel) -> None:
        self.channels[channel.name] = channel

    async def start_all(self) -> None:
        for channel in self.channels.values():
            await channel.start()

    async def stop_all(self) -> None:
        for channel in self.channels.values():
            await channel.stop()

    async def start_dispatcher(self) -> None:
        if self._dispatch_task and not self._dispatch_task.done():
            return
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

    async def stop_dispatcher(self) -> None:
        await cancel_background_task(self._dispatch_task)
        self._dispatch_task = None

    async def _dispatch_outbound(self) -> None:
        while True:
            # Cancellation cleanly exits this blocking wait.
            msg = await self.bus.consume_outbound()
            channel = self.channels.get(msg.channel)
            if not channel:
                logger.warning("Dropping outbound message: unknown channel '%s'", msg.channel)
                continue
            try:
                normalized = classify_outbound_message(msg.content, msg.metadata)
                msg.metadata = normalized.metadata
                if normalized.is_stream:
                    await channel.send_delta(msg.chat_id, msg.content, normalized.metadata)
                elif normalized.metadata.get("_streamed"):
                    continue
                else:
                    await channel.send(msg)
            except Exception:
                logger.exception(
                    "Failed sending outbound message via channel=%s chat_id=%s",
                    msg.channel,
                    msg.chat_id,
                )
