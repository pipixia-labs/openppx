"""Tests for Mochat channel adapter behavior."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sentientagent_v2.bus.events import OutboundMessage
from sentientagent_v2.bus.queue import MessageBus
from sentientagent_v2.channels.mochat import MochatChannel


class MochatChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_session_target_calls_session_api(self) -> None:
        bus = MessageBus()
        channel = MochatChannel(
            bus=bus,
            base_url="https://mochat.io",
            claw_token="claw-token",
        )

        with patch.object(channel, "_post_json", new=AsyncMock()) as post_json:
            await channel.send(
                OutboundMessage(
                    channel="mochat",
                    chat_id="session_123",
                    content="hello session",
                )
            )

        post_json.assert_awaited_once_with(
            "/api/claw/sessions/send",
            {"sessionId": "session_123", "content": "hello session"},
        )

    async def test_send_panel_target_calls_panel_api(self) -> None:
        bus = MessageBus()
        channel = MochatChannel(
            bus=bus,
            base_url="https://mochat.io",
            claw_token="claw-token",
        )

        with patch.object(channel, "_post_json", new=AsyncMock()) as post_json:
            await channel.send(
                OutboundMessage(
                    channel="mochat",
                    chat_id="panel:group_123",
                    content="hello panel",
                    metadata={"group_id": "workspace_1"},
                )
            )

        post_json.assert_awaited_once_with(
            "/api/claw/groups/panels/send",
            {"panelId": "group_123", "content": "hello panel", "groupId": "workspace_1"},
        )

    async def test_poll_once_publishes_allowed_session_inbound(self) -> None:
        bus = MessageBus()
        channel = MochatChannel(
            bus=bus,
            base_url="https://mochat.io",
            claw_token="claw-token",
            sessions=["session_1"],
            allow_from=["u02"],
        )

        with patch.object(
            channel,
            "_post_json",
            new=AsyncMock(
                return_value={
                    "cursor": 2,
                    "messages": [
                        {"messageId": "m1", "author": "u01", "content": "denied"},
                        {"messageId": "m2", "author": "u02", "content": "allowed"},
                    ],
                }
            ),
        ):
            await channel._poll_once()

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.channel, "mochat")
        self.assertEqual(inbound.chat_id, "session_1")
        self.assertEqual(inbound.sender_id, "u02")
        self.assertEqual(inbound.content, "allowed")
        self.assertEqual(inbound.metadata.get("message_id"), "m2")
        self.assertEqual(channel._session_cursors.get("session_1"), 2)

    async def test_poll_once_publishes_panel_inbound(self) -> None:
        bus = MessageBus()
        channel = MochatChannel(
            bus=bus,
            base_url="https://mochat.io",
            claw_token="claw-token",
            panels=["panel_1"],
        )

        with patch.object(
            channel,
            "_post_json",
            new=AsyncMock(
                return_value={
                    "groupId": "workspace_1",
                    "messages": [
                        {"messageId": "m_panel_1", "author": "u01", "content": "panel hello"},
                    ],
                }
            ),
        ):
            await channel._poll_once()

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.channel, "mochat")
        self.assertEqual(inbound.chat_id, "panel:panel_1")
        self.assertEqual(inbound.sender_id, "u01")
        self.assertEqual(inbound.content, "panel hello")
        self.assertEqual(inbound.metadata.get("group_id"), "workspace_1")


if __name__ == "__main__":
    unittest.main()
