"""Tests for Slack channel adapter behavior."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from openpipixia.bus.events import OutboundMessage
from openpipixia.bus.queue import MessageBus
from openpipixia.channels.slack import SlackChannel


class SlackChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_posts_message(self) -> None:
        bus = MessageBus()
        channel = SlackChannel(bus=bus, bot_token="xoxb-token")

        with patch.object(channel, "_api_call", new=AsyncMock()) as api_call:
            await channel.send(
                OutboundMessage(
                    channel="slack",
                    chat_id="C01",
                    content="hello from slack",
                )
            )

        api_call.assert_awaited_once_with(
            "chat.postMessage",
            {
                "channel": "C01",
                "text": "hello from slack",
            },
        )

    async def test_poll_once_publishes_allowed_inbound(self) -> None:
        bus = MessageBus()
        channel = SlackChannel(
            bus=bus,
            bot_token="xoxb-token",
            poll_channels=["C01"],
            allow_from=["U02"],
        )

        with patch.object(
            channel,
            "_api_call",
            new=AsyncMock(
                return_value={
                    "messages": [
                        {"type": "message", "user": "U02", "text": "allowed", "ts": "1002.0"},
                        {"type": "message", "user": "U01", "text": "denied", "ts": "1001.0"},
                    ]
                }
            ),
        ):
            await channel._poll_once()

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.channel, "slack")
        self.assertEqual(inbound.chat_id, "C01")
        self.assertEqual(inbound.sender_id, "U02")
        self.assertEqual(inbound.content, "allowed")
        self.assertEqual(inbound.metadata.get("ts"), "1002.0")


if __name__ == "__main__":
    unittest.main()
