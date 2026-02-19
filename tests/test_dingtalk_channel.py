"""Tests for DingTalk channel adapter behavior."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sentientagent_v2.bus.events import OutboundMessage
from sentientagent_v2.bus.queue import MessageBus
from sentientagent_v2.channels.dingtalk import DingTalkChannel


class DingTalkChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_posts_message_when_token_available(self) -> None:
        bus = MessageBus()
        channel = DingTalkChannel(
            bus=bus,
            client_id="dt-app-id",
            client_secret="dt-app-secret",
        )

        with (
            patch.object(channel, "_get_access_token", new=AsyncMock(return_value="token-1")),
            patch.object(channel, "_api_call", new=AsyncMock()) as api_call,
        ):
            await channel.send(
                OutboundMessage(
                    channel="dingtalk",
                    chat_id="staff-1",
                    content="hello dingtalk",
                )
            )

        api_call.assert_awaited_once_with(
            "POST",
            "/v1.0/robot/oToMessages/batchSend",
            payload={
                "robotCode": "dt-app-id",
                "userIds": ["staff-1"],
                "msgKey": "sampleMarkdown",
                "msgParam": '{"text":"hello dingtalk","title":"sentientagent_v2 reply"}',
            },
            headers={"x-acs-dingtalk-access-token": "token-1"},
        )

    async def test_on_message_publishes_allowed_inbound(self) -> None:
        bus = MessageBus()
        channel = DingTalkChannel(
            bus=bus,
            client_id="dt-app-id",
            client_secret="dt-app-secret",
            allow_from=["u02"],
        )

        await channel._on_message(content="denied", sender_id="u01", sender_name="alice")
        await channel._on_message(content="allowed", sender_id="u02", sender_name="bob")

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.channel, "dingtalk")
        self.assertEqual(inbound.chat_id, "u02")
        self.assertEqual(inbound.sender_id, "u02")
        self.assertEqual(inbound.content, "allowed")
        self.assertEqual(inbound.metadata.get("sender_name"), "bob")

    async def test_start_initializes_stream_client_when_available(self) -> None:
        bus = MessageBus()

        class _FakeCredential:
            def __init__(self, client_id: str, client_secret: str) -> None:
                self.client_id = client_id
                self.client_secret = client_secret

        class _FakeStreamClient:
            def __init__(self, credential: _FakeCredential) -> None:
                self.credential = credential
                self.registered: list[tuple[str, object]] = []

            def register_callback_handler(self, topic: str, handler: object) -> None:
                self.registered.append((topic, handler))

            async def start(self) -> None:
                await asyncio.sleep(3600)

        class _FakeChatbotMessage:
            TOPIC = "chatbot.topic"

            @staticmethod
            def from_dict(_payload: dict) -> object:
                return SimpleNamespace(
                    text=SimpleNamespace(content=""),
                    sender_staff_id="",
                    sender_id="",
                    sender_nick="",
                )

        channel = DingTalkChannel(
            bus=bus,
            client_id="dt-app-id",
            client_secret="dt-app-secret",
        )
        with (
            patch("sentientagent_v2.channels.dingtalk.DINGTALK_AVAILABLE", True),
            patch("sentientagent_v2.channels.dingtalk.Credential", _FakeCredential),
            patch("sentientagent_v2.channels.dingtalk.DingTalkStreamClient", _FakeStreamClient),
            patch("sentientagent_v2.channels.dingtalk.ChatbotMessage", _FakeChatbotMessage),
        ):
            await channel.start()
            self.assertIsNotNone(channel._stream_client)
            self.assertIsNotNone(channel._stream_task)
            self.assertEqual(len(channel._stream_client.registered), 1)
            self.assertEqual(channel._stream_client.registered[0][0], "chatbot.topic")
            await channel.stop()

    async def test_process_stream_payload_uses_chatbot_message_parser(self) -> None:
        bus = MessageBus()
        channel = DingTalkChannel(
            bus=bus,
            client_id="dt-app-id",
            client_secret="dt-app-secret",
            allow_from=["u02"],
        )

        class _FakeChatbotMessage:
            TOPIC = "chatbot.topic"

            @staticmethod
            def from_dict(_payload: dict) -> object:
                return SimpleNamespace(
                    text=SimpleNamespace(content="hello from stream"),
                    sender_staff_id="u02",
                    sender_id="u02",
                    sender_nick="stream-bob",
                )

        with patch("sentientagent_v2.channels.dingtalk.ChatbotMessage", _FakeChatbotMessage):
            await channel._process_stream_payload({"text": {"content": "ignored"}})

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.channel, "dingtalk")
        self.assertEqual(inbound.chat_id, "u02")
        self.assertEqual(inbound.content, "hello from stream")
        self.assertEqual(inbound.metadata.get("sender_name"), "stream-bob")


if __name__ == "__main__":
    unittest.main()
