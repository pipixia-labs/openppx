"""Tests for Feishu channel adapter behavior."""

from __future__ import annotations

import asyncio
import types as pytypes
import unittest
from unittest.mock import AsyncMock, patch

from sentientagent_v2.bus.queue import MessageBus
from sentientagent_v2.channels.feishu import FeishuChannel


class FeishuChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_message_adds_thumbsup_reaction_and_forwards_group_text(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(bus=bus, app_id="app-id", app_secret="app-secret")
        data = pytypes.SimpleNamespace(
            event=pytypes.SimpleNamespace(
                message=pytypes.SimpleNamespace(
                    message_id="om_123",
                    chat_id="oc_group_1",
                    chat_type="group",
                    message_type="text",
                    content='{"text":"hello from feishu"}',
                ),
                sender=pytypes.SimpleNamespace(
                    sender_type="user",
                    sender_id=pytypes.SimpleNamespace(open_id="ou_user_1"),
                ),
            )
        )

        with patch.object(channel, "_add_reaction", new=AsyncMock()) as add_reaction:
            await channel._on_message(data)
            add_reaction.assert_awaited_once_with("om_123", "THUMBSUP")

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.sender_id, "ou_user_1")
        self.assertEqual(inbound.chat_id, "oc_group_1")
        self.assertEqual(inbound.content, "hello from feishu")
        self.assertEqual(inbound.metadata.get("message_id"), "om_123")

    async def test_on_message_ignores_bot_messages(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(bus=bus, app_id="app-id", app_secret="app-secret")
        data = pytypes.SimpleNamespace(
            event=pytypes.SimpleNamespace(
                message=pytypes.SimpleNamespace(
                    message_id="om_ignored",
                    chat_id="oc_group_2",
                    chat_type="group",
                    message_type="text",
                    content='{"text":"should be ignored"}',
                ),
                sender=pytypes.SimpleNamespace(
                    sender_type="bot",
                    sender_id=pytypes.SimpleNamespace(open_id="ou_bot_1"),
                ),
            )
        )

        with patch.object(channel, "_add_reaction", new=AsyncMock()) as add_reaction:
            await channel._on_message(data)
            add_reaction.assert_not_awaited()

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_inbound(), timeout=0.05)

    async def test_stop_handles_ws_client_without_stop_method(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(bus=bus, app_id="app-id", app_secret="app-secret")
        channel._running = True
        channel._ws_client = object()

        # Should not raise even when SDK client has no public stop/close API.
        await channel.stop()
        self.assertFalse(channel._running)


if __name__ == "__main__":
    unittest.main()
