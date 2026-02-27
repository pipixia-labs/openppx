"""Tests for v1 multi-agent routing and session isolation."""

from __future__ import annotations

import unittest

from openheron.bus.events import InboundMessage
from openheron.runtime.agent_routing import AgentRouter


class AgentRoutingTests(unittest.TestCase):
    def test_default_route_without_bindings(self) -> None:
        router = AgentRouter(
            {
                "agents": {
                    "list": [
                        {"id": "main", "default": True, "workspace": "/tmp/main", "agentDir": "/tmp/main/agent"}
                    ]
                }
            }
        )
        msg = InboundMessage(channel="whatsapp", sender_id="u1", chat_id="+15550001", content="hi")
        resolved = router.resolve(msg)

        self.assertEqual(resolved.agent_id, "main")
        self.assertEqual(resolved.matched_by, "default")
        self.assertEqual(resolved.session_id, "agent:main:whatsapp:default:direct:+15550001")

    def test_binding_precedence_peer_over_account_over_channel(self) -> None:
        router = AgentRouter(
            {
                "agents": {
                    "list": [
                        {"id": "main", "default": True, "workspace": "/tmp/main", "agentDir": "/tmp/main/agent"},
                        {"id": "acct", "workspace": "/tmp/acct", "agentDir": "/tmp/acct/agent"},
                        {"id": "peer", "workspace": "/tmp/peer", "agentDir": "/tmp/peer/agent"},
                        {"id": "chan", "workspace": "/tmp/chan", "agentDir": "/tmp/chan/agent"},
                    ]
                },
                "bindings": [
                    {"agentId": "chan", "match": {"channel": "whatsapp"}},
                    {"agentId": "acct", "match": {"channel": "whatsapp", "accountId": "biz"}},
                    {
                        "agentId": "peer",
                        "match": {
                            "channel": "whatsapp",
                            "accountId": "biz",
                            "peer": {"kind": "direct", "id": "+15550009"},
                        },
                    },
                ],
            }
        )

        peer_msg = InboundMessage(
            channel="whatsapp",
            sender_id="u1",
            chat_id="+15550009",
            content="hi",
            metadata={"accountId": "biz", "peer": {"kind": "direct", "id": "+15550009"}},
        )
        account_msg = InboundMessage(
            channel="whatsapp",
            sender_id="u1",
            chat_id="+15551111",
            content="hi",
            metadata={"accountId": "biz", "peer": {"kind": "direct", "id": "+15551111"}},
        )
        channel_msg = InboundMessage(
            channel="whatsapp",
            sender_id="u1",
            chat_id="+15552222",
            content="hi",
            metadata={"peer": {"kind": "direct", "id": "+15552222"}},
        )

        self.assertEqual(router.resolve(peer_msg).agent_id, "peer")
        self.assertEqual(router.resolve(peer_msg).matched_by, "binding.peer")

        self.assertEqual(router.resolve(account_msg).agent_id, "acct")
        self.assertEqual(router.resolve(account_msg).matched_by, "binding.account")

        self.assertEqual(router.resolve(channel_msg).agent_id, "chan")
        self.assertEqual(router.resolve(channel_msg).matched_by, "binding.channel")

    def test_dm_sessions_are_per_peer_and_account_aware(self) -> None:
        router = AgentRouter(
            {
                "agents": {
                    "list": [
                        {"id": "main", "default": True, "workspace": "/tmp/main", "agentDir": "/tmp/main/agent"}
                    ]
                }
            }
        )

        msg_a = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="123",
            content="a",
            metadata={"accountId": "default", "peer": {"kind": "direct", "id": "123"}},
        )
        msg_b = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="456",
            content="b",
            metadata={"accountId": "default", "peer": {"kind": "direct", "id": "456"}},
        )
        msg_c = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="123",
            content="c",
            metadata={"accountId": "work", "peer": {"kind": "direct", "id": "123"}},
        )

        session_a = router.resolve(msg_a).session_id
        session_b = router.resolve(msg_b).session_id
        session_c = router.resolve(msg_c).session_id

        self.assertNotEqual(session_a, session_b)
        self.assertNotEqual(session_a, session_c)

    def test_same_channel_routes_by_account_id(self) -> None:
        router = AgentRouter(
            {
                "agents": {
                    "list": [
                        {"id": "main", "default": True, "workspace": "/tmp/main", "agentDir": "/tmp/main/agent"},
                        {"id": "personal", "workspace": "/tmp/personal", "agentDir": "/tmp/personal/agent"},
                        {"id": "business", "workspace": "/tmp/business", "agentDir": "/tmp/business/agent"},
                    ]
                },
                "bindings": [
                    {"agentId": "personal", "match": {"channel": "whatsapp", "accountId": "personal"}},
                    {"agentId": "business", "match": {"channel": "whatsapp", "accountId": "business"}},
                ],
            }
        )
        personal_msg = InboundMessage(
            channel="whatsapp",
            sender_id="u1",
            chat_id="+10001",
            content="hi",
            metadata={"accountId": "personal", "peer": {"kind": "direct", "id": "+10001"}},
        )
        business_msg = InboundMessage(
            channel="whatsapp",
            sender_id="u1",
            chat_id="+10001",
            content="hi",
            metadata={"accountId": "business", "peer": {"kind": "direct", "id": "+10001"}},
        )

        self.assertEqual(router.resolve(personal_msg).agent_id, "personal")
        self.assertEqual(router.resolve(business_msg).agent_id, "business")


if __name__ == "__main__":
    unittest.main()
