"""Tests for Discord channel adapter behavior."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from openpipixia.bus.events import OutboundMessage
from openpipixia.bus.queue import MessageBus
from openpipixia.channels.discord import DiscordChannel


class DiscordChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_posts_message(self) -> None:
        bus = MessageBus()
        channel = DiscordChannel(bus=bus, token="discord-token")

        with patch.object(channel, "_api_call", new=AsyncMock()) as api_call:
            await channel.send(
                OutboundMessage(
                    channel="discord",
                    chat_id="123",
                    content="hello discord",
                )
            )

        api_call.assert_awaited_once_with(
            "POST",
            "/channels/123/messages",
            payload={"content": "hello discord"},
            query=None,
        )

    async def test_poll_once_publishes_allowed_inbound(self) -> None:
        bus = MessageBus()
        channel = DiscordChannel(
            bus=bus,
            token="discord-token",
            poll_channels=["123"],
            allow_from=["u2"],
        )

        with patch.object(
            channel,
            "_api_call",
            new=AsyncMock(
                return_value=[
                    {
                        "id": "102",
                        "content": "allowed",
                        "author": {"id": "u2", "username": "bob"},
                    },
                    {
                        "id": "101",
                        "content": "denied",
                        "author": {"id": "u1", "username": "alice"},
                    },
                ]
            ),
        ):
            await channel._poll_once()

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.channel, "discord")
        self.assertEqual(inbound.chat_id, "123")
        self.assertEqual(inbound.sender_id, "u2")
        self.assertEqual(inbound.content, "allowed")
        self.assertEqual(inbound.metadata.get("message_id"), "102")


if __name__ == "__main__":
    unittest.main()
