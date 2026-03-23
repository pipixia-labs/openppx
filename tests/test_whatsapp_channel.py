"""Tests for WhatsApp channel adapter behavior."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openpipixia.bus.events import OutboundMessage
from openpipixia.bus.queue import MessageBus
from openpipixia.channels.whatsapp import WhatsAppChannel


class WhatsAppChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_writes_bridge_command_when_connected(self) -> None:
        bus = MessageBus()
        channel = WhatsAppChannel(bus=bus, bridge_url="ws://127.0.0.1:3001")
        channel._ws = SimpleNamespace(send=AsyncMock())
        channel._connected = True

        await channel.send(
            OutboundMessage(
                channel="whatsapp",
                chat_id="8613800138000@s.whatsapp.net",
                content="hello whatsapp",
            )
        )

        channel._ws.send.assert_awaited_once()
        payload = json.loads(channel._ws.send.await_args.args[0])
        self.assertEqual(payload["type"], "send")
        self.assertEqual(payload["to"], "8613800138000@s.whatsapp.net")
        self.assertEqual(payload["text"], "hello whatsapp")

    async def test_handle_bridge_message_publishes_allowed_inbound(self) -> None:
        bus = MessageBus()
        channel = WhatsAppChannel(
            bus=bus,
            bridge_url="ws://127.0.0.1:3001",
            allow_from=["8613800138000"],
        )

        await channel._handle_bridge_message(
            json.dumps(
                {
                    "type": "message",
                    "id": "m-denied",
                    "sender": "8613700137000@s.whatsapp.net",
                    "content": "denied",
                    "timestamp": 1700000000,
                }
            )
        )
        await channel._handle_bridge_message(
            json.dumps(
                {
                    "type": "message",
                    "id": "m-allowed",
                    "sender": "8613800138000@s.whatsapp.net",
                    "content": "allowed",
                    "timestamp": 1700000001,
                }
            )
        )

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.channel, "whatsapp")
        self.assertEqual(inbound.sender_id, "8613800138000")
        self.assertEqual(inbound.chat_id, "8613800138000@s.whatsapp.net")
        self.assertEqual(inbound.content, "allowed")
        self.assertEqual(inbound.metadata.get("message_id"), "m-allowed")


if __name__ == "__main__":
    unittest.main()
