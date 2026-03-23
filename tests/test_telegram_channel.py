"""Tests for Telegram channel adapter behavior."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from openpipixia.bus.events import OutboundMessage
from openpipixia.bus.queue import MessageBus
from openpipixia.channels.telegram import TelegramChannel


class TelegramChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_posts_message(self) -> None:
        bus = MessageBus()
        channel = TelegramChannel(bus=bus, token="telegram-token")
        with patch.object(channel, "_api_call", new=AsyncMock()) as api_call:
            await channel.send(OutboundMessage(channel="telegram", chat_id="123", content="hello"))
        api_call.assert_awaited_once_with(
            "sendMessage",
            {"chat_id": "123", "text": "hello"},
        )

    async def test_process_update_publishes_inbound(self) -> None:
        bus = MessageBus()
        channel = TelegramChannel(bus=bus, token="telegram-token")
        update = {
            "update_id": 1001,
            "message": {
                "message_id": 88,
                "date": 1739875200,
                "chat": {"id": 90001, "type": "private"},
                "from": {"id": 70001, "username": "alice"},
                "text": "hello from telegram",
            },
        }

        await channel._process_update(update)
        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.channel, "telegram")
        self.assertEqual(inbound.chat_id, "90001")
        self.assertEqual(inbound.content, "hello from telegram")
        self.assertEqual(inbound.sender_id, "70001|@alice")
        self.assertEqual(inbound.metadata.get("message_id"), "88")

    async def test_process_update_respects_allow_from(self) -> None:
        bus = MessageBus()
        channel = TelegramChannel(
            bus=bus,
            token="telegram-token",
            allow_from=["70002"],
        )
        update = {
            "update_id": 1002,
            "message": {
                "message_id": 89,
                "chat": {"id": 90002, "type": "private"},
                "from": {"id": 70001, "username": "alice"},
                "text": "should be blocked",
            },
        }

        await channel._process_update(update)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_inbound(), timeout=0.05)


if __name__ == "__main__":
    unittest.main()
