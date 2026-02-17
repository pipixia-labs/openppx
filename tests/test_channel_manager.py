"""Tests for ChannelManager dispatch resilience."""

from __future__ import annotations

import asyncio
import unittest

from sentientagent_v2.bus.events import OutboundMessage
from sentientagent_v2.bus.queue import MessageBus
from sentientagent_v2.channels.base import BaseChannel
from sentientagent_v2.channels.manager import ChannelManager


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


if __name__ == "__main__":
    unittest.main()
