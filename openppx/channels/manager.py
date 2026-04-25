"""Channel manager for outbound routing."""

from __future__ import annotations

import asyncio
import logging

from ..bus.events import OutboundMessage
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
        pending: list[OutboundMessage] = []
        while True:
            # Cancellation cleanly exits this blocking wait.
            msg = pending.pop(0) if pending else await self.bus.consume_outbound()
            channel = self.channels.get(msg.channel)
            if not channel:
                logger.warning("Dropping outbound message: unknown channel '%s'", msg.channel)
                continue
            try:
                normalized = classify_outbound_message(msg.content, msg.metadata)
                msg.metadata = normalized.metadata
                if normalized.event_class == "stream_delta" and not normalized.metadata.get("_stream_end"):
                    msg, extra_pending = self._coalesce_stream_deltas(msg)
                    pending.extend(extra_pending)
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

    def _coalesce_stream_deltas(self, first_msg: OutboundMessage) -> tuple[OutboundMessage, list[OutboundMessage]]:
        """Merge queued consecutive stream deltas for the same channel target."""

        target_key = (first_msg.channel, first_msg.chat_id)
        combined_content = first_msg.content
        combined_metadata = dict(first_msg.metadata or {})
        pending: list[OutboundMessage] = []

        while True:
            try:
                next_msg = self.bus.outbound.get_nowait()
            except asyncio.QueueEmpty:
                break

            next_normalized = classify_outbound_message(next_msg.content, next_msg.metadata)
            next_msg.metadata = next_normalized.metadata
            same_target = (next_msg.channel, next_msg.chat_id) == target_key
            is_plain_delta = next_normalized.event_class == "stream_delta" and not next_normalized.metadata.get(
                "_stream_end"
            )
            if same_target and is_plain_delta:
                combined_content += next_msg.content
                combined_metadata.update(next_normalized.metadata)
                continue

            pending.append(next_msg)
            break

        return (
            OutboundMessage(
                channel=first_msg.channel,
                chat_id=first_msg.chat_id,
                content=combined_content,
                reply_to=first_msg.reply_to,
                metadata=combined_metadata,
            ),
            pending,
        )
