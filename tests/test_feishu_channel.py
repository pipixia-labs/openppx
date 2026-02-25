"""Tests for Feishu channel adapter behavior."""

from __future__ import annotations

import asyncio
import types as pytypes
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from openheron.bus.events import OutboundMessage
from openheron.bus.queue import MessageBus
from openheron.channels.feishu import FeishuChannel


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

    async def test_on_message_respects_allow_from(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(
            bus=bus,
            app_id="app-id",
            app_secret="app-secret",
            allow_from=["ou_allowed"],
        )
        data = pytypes.SimpleNamespace(
            event=pytypes.SimpleNamespace(
                message=pytypes.SimpleNamespace(
                    message_id="om_blocked_1",
                    chat_id="oc_group_2",
                    chat_type="group",
                    message_type="text",
                    content='{"text":"should be blocked"}',
                ),
                sender=pytypes.SimpleNamespace(
                    sender_type="user",
                    sender_id=pytypes.SimpleNamespace(open_id="ou_denied"),
                ),
            )
        )

        with patch.object(channel, "_add_reaction", new=AsyncMock()) as add_reaction:
            await channel._on_message(data)
            add_reaction.assert_not_awaited()

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_inbound(), timeout=0.05)

    async def test_on_message_downloads_file_and_forwards_workspace_path(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(bus=bus, app_id="app-id", app_secret="app-secret")
        data = pytypes.SimpleNamespace(
            event=pytypes.SimpleNamespace(
                message=pytypes.SimpleNamespace(
                    message_id="om_file_1",
                    chat_id="oc_group_3",
                    chat_type="group",
                    message_type="file",
                    content='{"file_key":"file_v2_123","file_name":"report.pdf"}',
                ),
                sender=pytypes.SimpleNamespace(
                    sender_type="user",
                    sender_id=pytypes.SimpleNamespace(open_id="ou_user_2"),
                ),
            )
        )
        saved = Path("/tmp/inbox/feishu/report.pdf")

        with (
            patch.object(channel, "_add_reaction", new=AsyncMock()),
            patch.object(channel, "_download_file_sync", return_value=saved) as download_file,
        ):
            await channel._on_message(data)

        download_file.assert_called_once_with("file_v2_123", "report.pdf", "om_file_1")
        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertIn(str(saved), inbound.content)
        self.assertEqual(inbound.metadata.get("msg_type"), "file")
        self.assertEqual(inbound.metadata.get("file_key"), "file_v2_123")
        self.assertEqual(inbound.metadata.get("local_path"), str(saved))

    async def test_on_message_downloads_image_and_forwards_workspace_path(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(bus=bus, app_id="app-id", app_secret="app-secret")
        data = pytypes.SimpleNamespace(
            event=pytypes.SimpleNamespace(
                message=pytypes.SimpleNamespace(
                    message_id="om_img_1",
                    chat_id="oc_group_4",
                    chat_type="group",
                    message_type="image",
                    content='{"image_key":"img_v2_001"}',
                ),
                sender=pytypes.SimpleNamespace(
                    sender_type="user",
                    sender_id=pytypes.SimpleNamespace(open_id="ou_user_3"),
                ),
            )
        )
        saved = Path("/tmp/inbox/feishu/photo.png")

        with (
            patch.object(channel, "_add_reaction", new=AsyncMock()),
            patch.object(channel, "_download_image_sync", return_value=saved) as download_image,
        ):
            await channel._on_message(data)

        download_image.assert_called_once_with("img_v2_001", "om_img_1")
        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertIn(str(saved), inbound.content)
        self.assertEqual(inbound.metadata.get("msg_type"), "image")
        self.assertEqual(inbound.metadata.get("image_key"), "img_v2_001")
        self.assertEqual(inbound.metadata.get("local_path"), str(saved))

    async def test_on_post_message_with_text_and_image_forwards_both(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(bus=bus, app_id="app-id", app_secret="app-secret")
        data = pytypes.SimpleNamespace(
            event=pytypes.SimpleNamespace(
                message=pytypes.SimpleNamespace(
                    message_id="om_post_1",
                    chat_id="oc_group_5",
                    chat_type="group",
                    message_type="post",
                    content='{"zh_cn":{"title":"","content":[[{"tag":"text","text":"please check this image"},{"tag":"img","image_key":"img_v2_post_1"}]]}}',
                ),
                sender=pytypes.SimpleNamespace(
                    sender_type="user",
                    sender_id=pytypes.SimpleNamespace(open_id="ou_user_4"),
                ),
            )
        )
        saved = Path("/tmp/inbox/feishu/post.png")

        with (
            patch.object(channel, "_add_reaction", new=AsyncMock()),
            patch.object(channel, "_download_image_sync", return_value=saved) as download_image,
        ):
            await channel._on_message(data)

        download_image.assert_called_once_with("img_v2_post_1", "om_post_1")
        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        self.assertIn("please check this image", inbound.content)
        self.assertIn(str(saved), inbound.content)
        self.assertEqual(inbound.metadata.get("msg_type"), "post")
        self.assertEqual(inbound.metadata.get("image_keys"), ["img_v2_post_1"])
        self.assertEqual(inbound.metadata.get("image_paths"), [str(saved)])

    async def test_stop_handles_ws_client_without_stop_method(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(bus=bus, app_id="app-id", app_secret="app-secret")
        channel._running = True
        channel._ws_client = object()

        # Should not raise even when SDK client has no public stop/close API.
        await channel.stop()
        self.assertFalse(channel._running)

    async def test_send_sync_routes_image_metadata(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(bus=bus, app_id="app-id", app_secret="app-secret")
        channel._client = object()
        outbound = OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content="image caption",
            metadata={"content_type": "image", "image_path": "/tmp/demo.png"},
        )

        with (
            patch.object(channel, "_send_image_sync", return_value="om_image_1") as send_image,
            patch.object(channel, "_send_text_sync", return_value="om_text_1") as send_text,
        ):
            channel._send_sync(outbound)

        send_image.assert_called_once_with(outbound, "/tmp/demo.png")
        send_text.assert_called_once_with(outbound, "image caption")
        self.assertEqual(
            outbound.metadata.get("delivery"),
            {"status": "sent", "content_type": "image", "message_ids": ["om_image_1", "om_text_1"]},
        )

    async def test_send_sync_falls_back_to_text_when_image_send_fails(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(bus=bus, app_id="app-id", app_secret="app-secret")
        channel._client = object()
        outbound = OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content="",
            metadata={"content_type": "image", "image_path": "/tmp/demo.png"},
        )

        with (
            patch.object(channel, "_send_image_sync", side_effect=RuntimeError("upload failed")),
            patch.object(channel, "_send_text_sync", return_value="om_fallback_1") as send_text,
        ):
            channel._send_sync(outbound)

        send_text.assert_called_once_with(outbound, "[image send failed] /tmp/demo.png")
        self.assertEqual(
            outbound.metadata.get("delivery"),
            {"status": "fallback_text", "content_type": "image", "message_ids": ["om_fallback_1"]},
        )

    async def test_send_sync_routes_file_metadata(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(bus=bus, app_id="app-id", app_secret="app-secret")
        channel._client = object()
        outbound = OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content="attachment caption",
            metadata={"content_type": "file", "file_path": "/tmp/report.docx"},
        )

        with (
            patch.object(channel, "_send_file_sync", return_value="om_file_1") as send_file,
            patch.object(channel, "_send_text_sync", return_value="om_text_2") as send_text,
        ):
            channel._send_sync(outbound)

        send_file.assert_called_once_with(outbound, "/tmp/report.docx")
        send_text.assert_called_once_with(outbound, "attachment caption")
        self.assertEqual(
            outbound.metadata.get("delivery"),
            {"status": "sent", "content_type": "file", "message_ids": ["om_file_1", "om_text_2"]},
        )

    async def test_send_sync_falls_back_to_text_when_file_send_fails(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(bus=bus, app_id="app-id", app_secret="app-secret")
        channel._client = object()
        outbound = OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content="",
            metadata={"content_type": "file", "file_path": "/tmp/report.docx"},
        )

        with (
            patch.object(channel, "_send_file_sync", side_effect=RuntimeError("upload failed")),
            patch.object(channel, "_send_text_sync", return_value="om_fallback_2") as send_text,
        ):
            channel._send_sync(outbound)

        send_text.assert_called_once_with(outbound, "[file send failed] /tmp/report.docx")
        self.assertEqual(
            outbound.metadata.get("delivery"),
            {"status": "fallback_text", "content_type": "file", "message_ids": ["om_fallback_2"]},
        )

    async def test_send_sync_records_text_delivery_metadata(self) -> None:
        bus = MessageBus()
        channel = FeishuChannel(bus=bus, app_id="app-id", app_secret="app-secret")
        channel._client = object()
        outbound = OutboundMessage(channel="feishu", chat_id="oc_group_1", content="plain text")

        with patch.object(channel, "_send_text_sync", return_value="om_text_plain") as send_text:
            channel._send_sync(outbound)

        send_text.assert_called_once_with(outbound)
        self.assertEqual(
            outbound.metadata.get("delivery"),
            {"status": "sent", "content_type": "text", "message_ids": ["om_text_plain"]},
        )


if __name__ == "__main__":
    unittest.main()
