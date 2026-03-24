"""Tests for Weixin channel adapter behavior."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openpipixia.bus.events import OutboundMessage
from openpipixia.bus.queue import MessageBus
from openpipixia.channels.weixin import WeixinChannel


class WeixinChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_message_publishes_allowed_inbound_and_caches_context(self) -> None:
        bus = MessageBus()
        channel = WeixinChannel(bus=bus, allow_from=["wx-02"])

        await channel._process_message(
            {
                "message_id": "m-1",
                "from_user_id": "wx-01",
                "context_token": "ctx-1",
                "item_list": [{"type": 1, "text_item": {"text": "denied"}}],
            }
        )
        await channel._process_message(
            {
                "message_id": "m-2",
                "from_user_id": "wx-02",
                "context_token": "ctx-2",
                "item_list": [{"type": 1, "text_item": {"text": "allowed"}}],
            }
        )

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.channel, "weixin")
        self.assertEqual(inbound.chat_id, "wx-02")
        self.assertEqual(inbound.sender_id, "wx-02")
        self.assertEqual(inbound.content, "allowed")
        self.assertEqual(inbound.metadata.get("message_id"), "m-2")
        self.assertEqual(channel._context_tokens["wx-02"], "ctx-2")

    async def test_send_uses_cached_context_token(self) -> None:
        bus = MessageBus()
        channel = WeixinChannel(bus=bus, token="token-1")
        channel._client = SimpleNamespace()
        channel._context_tokens["wx-02"] = "ctx-2"

        with patch.object(channel, "_send_text", new=AsyncMock()) as mocked_send:
            await channel.send(
                OutboundMessage(
                    channel="weixin",
                    chat_id="wx-02",
                    content="hello weixin",
                )
            )

        mocked_send.assert_awaited_once_with("wx-02", "hello weixin", "ctx-2")

    async def test_send_image_uses_media_upload_then_text(self) -> None:
        bus = MessageBus()
        channel = WeixinChannel(bus=bus, token="token-1")
        channel._client = SimpleNamespace()
        channel._context_tokens["wx-02"] = "ctx-2"

        with (
            patch.object(channel, "_send_media_file", new=AsyncMock()) as mocked_media,
            patch.object(channel, "_send_text", new=AsyncMock()) as mocked_text,
        ):
            await channel.send(
                OutboundMessage(
                    channel="weixin",
                    chat_id="wx-02",
                    content="caption",
                    metadata={"content_type": "image", "image_path": "demo.png"},
                )
            )

        mocked_media.assert_awaited_once_with("wx-02", "demo.png", "ctx-2")
        mocked_text.assert_awaited_once_with("wx-02", "caption", "ctx-2")

    async def test_process_message_downloads_image_and_sets_media_list(self) -> None:
        bus = MessageBus()
        channel = WeixinChannel(bus=bus)

        with patch.object(channel, "_download_media_item", new=AsyncMock(return_value="/tmp/image.png")):
            await channel._process_message(
                {
                    "message_id": "m-3",
                    "from_user_id": "wx-03",
                    "context_token": "ctx-3",
                    "item_list": [{"type": 2, "image_item": {"media": {"encrypt_query_param": "abc"}}}],
                }
            )

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertEqual(inbound.media, ["/tmp/image.png"])
        self.assertIn("[Image: source: /tmp/image.png]", inbound.content)

    async def test_login_loads_existing_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "account.json").write_text(
                json.dumps(
                    {
                        "token": "saved-token",
                        "get_updates_buf": "cursor-1",
                        "base_url": "https://example.weixin",
                    }
                ),
                encoding="utf-8",
            )
            channel = WeixinChannel(bus=MessageBus(), state_dir=str(state_dir))

            ok = await channel.login()

        self.assertTrue(ok)
        self.assertEqual(channel._token, "saved-token")
        self.assertEqual(channel._get_updates_buf, "cursor-1")
        self.assertEqual(channel.base_url, "https://example.weixin")


if __name__ == "__main__":
    unittest.main()
