"""Tests for WhatsApp channel adapter behavior."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openheron.bus.events import OutboundMessage
from openheron.bus.queue import MessageBus
from openheron.channels.whatsapp import WhatsAppChannel
from openheron.runtime.agent_routing import AgentRouter


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

    async def test_handle_bridge_message_emits_account_and_peer_metadata_for_routing(self) -> None:
        bus = MessageBus()
        channel = WhatsAppChannel(bus=bus, bridge_url="ws://127.0.0.1:3001")

        await channel._handle_bridge_message(
            json.dumps(
                {
                    "type": "message",
                    "id": "m-account-peer",
                    "sender": "8613999998888@s.whatsapp.net",
                    "content": "hello",
                    "timestamp": 1700000010,
                    "accountId": "business",
                    "isGroup": False,
                }
            )
        )

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.metadata.get("accountId"), "business")
        self.assertEqual(inbound.metadata.get("peer_kind"), "direct")
        self.assertEqual(inbound.metadata.get("peer_id"), "8613999998888@s.whatsapp.net")
        self.assertEqual(inbound.metadata.get("chat_type"), "direct")
        self.assertEqual(
            inbound.metadata.get("peer"),
            {"kind": "direct", "id": "8613999998888@s.whatsapp.net"},
        )

        router = AgentRouter(
            {
                "agents": {
                    "list": [
                        {"id": "main", "default": True, "workspace": "/tmp/main", "agentDir": "/tmp/main/agent"},
                        {"id": "biz", "workspace": "/tmp/biz", "agentDir": "/tmp/biz/agent"},
                    ]
                },
                "bindings": [
                    {"agentId": "biz", "match": {"channel": "whatsapp", "accountId": "business"}},
                ],
            }
        )
        routed = router.resolve(inbound)
        self.assertEqual(routed.agent_id, "biz")


if __name__ == "__main__":
    unittest.main()
