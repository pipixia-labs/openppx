"""Tests for QQ channel adapter behavior."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openpipixia.bus.events import OutboundMessage
from openpipixia.bus.queue import MessageBus
from openpipixia.channels.qq import QQChannel


class QQChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_uses_client_api_when_initialized(self) -> None:
        bus = MessageBus()
        channel = QQChannel(bus=bus, app_id="app-id", secret="app-secret")
        channel._client = SimpleNamespace(api=SimpleNamespace(post_c2c_message=AsyncMock()))

        await channel.send(
            OutboundMessage(
                channel="qq",
                chat_id="openid-1",
                content="hello qq",
            )
        )

        channel._client.api.post_c2c_message.assert_awaited_once_with(
            openid="openid-1",
            msg_type=0,
            content="hello qq",
        )

    async def test_on_message_publishes_allowed_inbound(self) -> None:
        bus = MessageBus()
        channel = QQChannel(
            bus=bus,
            app_id="app-id",
            secret="app-secret",
            allow_from=["u02"],
        )

        denied = SimpleNamespace(
            id="m-1",
            content="denied",
            author=SimpleNamespace(id="u01"),
        )
        allowed = SimpleNamespace(
            id="m-2",
            content="allowed",
            author=SimpleNamespace(id="u02"),
        )

        await channel._on_message(denied)
        await channel._on_message(allowed)

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.channel, "qq")
        self.assertEqual(inbound.chat_id, "u02")
        self.assertEqual(inbound.sender_id, "u02")
        self.assertEqual(inbound.content, "allowed")
        self.assertEqual(inbound.metadata.get("message_id"), "m-2")


if __name__ == "__main__":
    unittest.main()
