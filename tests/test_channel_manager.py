"""Tests for ChannelManager dispatch resilience."""

from __future__ import annotations

import asyncio
import unittest

from openppx.bus.events import OutboundMessage
from openppx.bus.queue import MessageBus
from openppx.channels.base import BaseChannel
from openppx.channels.manager import ChannelManager


class _FlakyChannel(BaseChannel):
    name = "flaky"

    def __init__(self, bus: MessageBus) -> None:
        super().__init__(bus)
        self.attempts = 0
        self.sent: list[str] = []

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("synthetic send failure")
        self.sent.append(msg.content)


class _DeltaChannel(BaseChannel):
    name = "delta"

    def __init__(self, bus: MessageBus) -> None:
        super().__init__(bus)
        self.deltas: list[tuple[str, str, dict]] = []
        self.sent: list[str] = []

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg.content)

    async def send_delta(self, chat_id: str, delta: str, metadata: dict | None = None) -> None:
        self.deltas.append((chat_id, delta, dict(metadata or {})))


class ChannelManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatcher_survives_channel_send_exception(self) -> None:
        bus = MessageBus()
        manager = ChannelManager(bus)
        channel = _FlakyChannel(bus)
        manager.register(channel)

        await manager.start_dispatcher()
        try:
            await bus.publish_outbound(OutboundMessage(channel="flaky", chat_id="c1", content="first"))
            await bus.publish_outbound(OutboundMessage(channel="flaky", chat_id="c1", content="second"))

            # Give dispatcher enough time to consume both queued messages.
            await asyncio.sleep(0.1)
            self.assertEqual(channel.sent, ["second"])
            self.assertEqual(channel.attempts, 2)
        finally:
            await manager.stop_dispatcher()

    async def test_coalesce_stream_deltas_merges_same_target(self) -> None:
        bus = MessageBus()
        manager = ChannelManager(bus)
        await bus.publish_outbound(
            OutboundMessage(channel="delta", chat_id="c1", content="Hello", metadata={"_stream_delta": True})
        )
        await bus.publish_outbound(
            OutboundMessage(channel="delta", chat_id="c1", content=" world", metadata={"_stream_delta": True})
        )
        await bus.publish_outbound(
            OutboundMessage(channel="delta", chat_id="c1", content="!", metadata={"_stream_delta": True})
        )

        first = await bus.consume_outbound()
        merged, pending = manager._coalesce_stream_deltas(first)

        self.assertEqual(merged.content, "Hello world!")
        self.assertTrue(merged.metadata.get("_stream_delta"))
        self.assertEqual(pending, [])

    async def test_coalesce_stream_deltas_keeps_stream_end_as_boundary(self) -> None:
        bus = MessageBus()
        manager = ChannelManager(bus)
        await bus.publish_outbound(
            OutboundMessage(channel="delta", chat_id="c1", content="Hello", metadata={"_stream_delta": True})
        )
        await bus.publish_outbound(
            OutboundMessage(channel="delta", chat_id="c1", content="", metadata={"_stream_end": True})
        )
        await bus.publish_outbound(
            OutboundMessage(channel="delta", chat_id="c1", content="next", metadata={"_stream_delta": True})
        )

        first = await bus.consume_outbound()
        merged, pending = manager._coalesce_stream_deltas(first)

        self.assertEqual(merged.content, "Hello")
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].metadata.get("_stream_end"))
        remaining = await bus.consume_outbound()
        self.assertEqual(remaining.content, "next")

    async def test_dispatcher_sends_coalesced_stream_delta(self) -> None:
        bus = MessageBus()
        manager = ChannelManager(bus)
        channel = _DeltaChannel(bus)
        manager.register(channel)

        await manager.start_dispatcher()
        try:
            await bus.publish_outbound(
                OutboundMessage(channel="delta", chat_id="c1", content="Hello", metadata={"_stream_delta": True})
            )
            await bus.publish_outbound(
                OutboundMessage(channel="delta", chat_id="c1", content=" world", metadata={"_stream_delta": True})
            )
            await bus.publish_outbound(
                OutboundMessage(channel="delta", chat_id="c1", content="!", metadata={"_stream_delta": True})
            )

            await asyncio.sleep(0.1)
            self.assertEqual(len(channel.deltas), 1)
            self.assertEqual(channel.deltas[0][0], "c1")
            self.assertEqual(channel.deltas[0][1], "Hello world!")
        finally:
            await manager.stop_dispatcher()


if __name__ == "__main__":
    unittest.main()
