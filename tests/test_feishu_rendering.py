"""Tests for Feishu rich rendering helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from openppx.bus.events import OutboundMessage
from openppx.bus.queue import MessageBus
from openppx.channels.feishu import (
    FeishuChannel,
    _build_compact_step_card,
    _build_step_card,
    _build_step_card_payload,
    _detect_msg_format,
    _markdown_to_post,
    _parse_md_table,
    _render_step_markdown,
    _split_elements_by_table_limit,
    _split_headings,
)


class FeishuRenderingTests(unittest.TestCase):
    def test_parse_md_table_strips_markdown_formatting(self) -> None:
        table = _parse_md_table(
            """
| **Name** | __Status__ |
| --- | --- |
| **Alice** | __Ready__ |
"""
        )

        assert table is not None
        self.assertEqual([col["display_name"] for col in table["columns"]], ["Name", "Status"])
        self.assertEqual(table["rows"], [{"c0": "Alice", "c1": "Ready"}])

    def test_split_headings_keeps_code_block(self) -> None:
        elements = _split_headings("# **Heading**\n\n```python\nprint('hi')\n```")

        self.assertEqual(elements[0]["tag"], "div")
        self.assertIn("**Heading**", elements[0]["text"]["content"])
        self.assertIn("```python", elements[1]["content"])

    def test_detect_msg_format_prefers_interactive_for_complex_markdown(self) -> None:
        self.assertEqual(_detect_msg_format("# Title\n\n```python\nprint('hi')\n```"), "interactive")
        self.assertEqual(_detect_msg_format("See [docs](https://example.com)"), "post")

    def test_markdown_to_post_preserves_links(self) -> None:
        payload = _markdown_to_post("See [docs](https://example.com)")

        self.assertIn('"tag": "a"', payload)
        self.assertIn('"href": "https://example.com"', payload)

    def test_render_step_markdown_contains_phase_and_title(self) -> None:
        text = _render_step_markdown(
            "Tool finished successfully.",
            {"_step_phase": "finished", "_step_title": "write_file"},
        )

        self.assertIn("write_file", text)
        self.assertIn("finished", text)

    def test_split_elements_by_table_limit_splits_multiple_tables(self) -> None:
        groups = _split_elements_by_table_limit(
            [
                {"tag": "markdown", "content": "intro"},
                {"tag": "table", "columns": [], "rows": [{"c0": "one"}], "page_size": 1},
                {"tag": "markdown", "content": "middle"},
                {"tag": "table", "columns": [], "rows": [{"c0": "two"}], "page_size": 1},
            ]
        )

        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0][1]["rows"][0]["c0"], "one")
        self.assertEqual(groups[1][-1]["rows"][0]["c0"], "two")

    def test_build_step_card_includes_status_and_kind(self) -> None:
        card = _build_step_card(
            "Finished successfully.",
            {
                "_step_title": "write_file",
                "_step_phase": "finished",
                "_step_kind": "tool",
                "_step_update_kind": "lifecycle",
                "_step_order": 1,
                "_event_seq": 2,
                "_task_id": "task-1",
            },
        )

        self.assertEqual(card["header"]["title"]["content"], "write_file")
        fields = card["elements"][0]["fields"]
        self.assertIn("finished", fields[0]["text"]["content"])
        self.assertIn("tool", fields[1]["text"]["content"])
        self.assertIn("lifecycle", fields[2]["text"]["content"])
        self.assertIn("1", fields[3]["text"]["content"])
        self.assertIn("2", fields[4]["text"]["content"])
        self.assertIn("task-1", fields[5]["text"]["content"])

    def test_build_compact_step_card_omits_debug_fields(self) -> None:
        card = _build_compact_step_card(
            "Process still running. Suggested next poll in ~1000ms.",
            {
                "_step_title": "Process still running",
                "_step_phase": "running",
                "_step_kind": "tool",
                "_step_update_kind": "progress",
                "_step_order": 1,
                "_event_seq": 2,
                "_task_id": "task-1",
            },
        )

        self.assertFalse(card["config"]["wide_screen_mode"])
        self.assertEqual(card["header"]["title"]["content"], "Running: Process still running")
        rendered = str(card)
        self.assertIn("Process still running", rendered)
        self.assertIn("task task-1", rendered)
        self.assertNotIn("**Status**", rendered)
        self.assertNotIn("**Kind**", rendered)
        self.assertNotIn("**Update**", rendered)
        self.assertNotIn("**Order**", rendered)
        self.assertNotIn("**Event**", rendered)

    def test_step_card_payload_defaults_to_compact(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            card = _build_step_card_payload(
                "Finished successfully.",
                {
                    "_step_title": "write_file",
                    "_step_phase": "finished",
                    "_step_kind": "tool",
                    "_step_update_kind": "lifecycle",
                    "_step_order": 1,
                    "_event_seq": 2,
                    "_task_id": "task-1",
                },
            )

        self.assertFalse(card["config"]["wide_screen_mode"])
        self.assertEqual(card["header"]["title"]["content"], "Done: write_file")
        self.assertNotIn("fields", card["elements"][0])

    def test_step_card_payload_debug_env_uses_verbose_card(self) -> None:
        with patch.dict("os.environ", {"OPENPPX_FEISHU_DEBUG_CARDS": "1"}):
            card = _build_step_card_payload(
                "Finished successfully.",
                {
                    "_step_title": "write_file",
                    "_step_phase": "finished",
                    "_step_kind": "tool",
                    "_step_update_kind": "lifecycle",
                    "_step_order": 1,
                    "_event_seq": 2,
                    "_task_id": "task-1",
                },
            )

        self.assertTrue(card["config"]["wide_screen_mode"])
        self.assertIn("fields", card["elements"][0])
        self.assertIn("**Status**", card["elements"][0]["fields"][0]["text"]["content"])

    def test_send_sync_uses_step_cards_for_step_events(self) -> None:
        channel = FeishuChannel(bus=MessageBus(), app_id="app-id", app_secret="app-secret")
        channel._client = object()
        outbound = OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content="Completed successfully.",
            metadata={
                "_event_class": "step_update",
                "_step_phase": "finished",
                "_step_title": "write_file",
            },
        )

        with patch.object(channel, "_send_step_cards_sync", return_value="om_step_1") as send_step_cards:
            channel._send_sync(outbound)

        send_step_cards.assert_called_once()
        args = send_step_cards.call_args.args
        self.assertEqual(args[1], "Completed successfully.")
        self.assertEqual(outbound.metadata["delivery"]["message_ids"], ["om_step_1"])

    def test_send_step_cards_patches_existing_message_for_same_step(self) -> None:
        channel = FeishuChannel(bus=MessageBus(), app_id="app-id", app_secret="app-secret")
        channel._client = object()
        metadata = {
            "_event_class": "step_update",
            "_step_phase": "running",
            "_step_title": "write_file",
            "_step_id": "step-1",
        }
        outbound = OutboundMessage(channel="feishu", chat_id="oc_group_1", content="Running...", metadata=metadata)

        with patch.object(channel, "_send_message_request_sync", return_value="om_step_1") as send_msg:
            first = channel._send_step_cards_sync(outbound, "Running...", metadata)

        with patch.object(channel, "_patch_message_sync") as patch_msg:
            second = channel._send_step_cards_sync(outbound, "Still running...", metadata)

        self.assertEqual(first, "om_step_1")
        self.assertEqual(second, "om_step_1")
        send_msg.assert_called_once()
        patch_msg.assert_called_once()

    def test_send_sync_suppresses_routine_adk_tool_lifecycle_cards(self) -> None:
        channel = FeishuChannel(bus=MessageBus(), app_id="app-id", app_secret="app-secret")
        channel._client = object()
        outbound = OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content="Finished `web_search`",
            metadata={
                "_event_class": "step_update",
                "_feedback_origin": "adk_plugin",
                "_step_kind": "tool",
                "_step_update_kind": "lifecycle",
                "_step_phase": "finished",
                "_step_title": "web_search",
                "_step_id": "step-1",
            },
        )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(channel, "_send_message_request_sync") as send_msg,
            patch.object(channel, "_patch_message_sync") as patch_msg,
        ):
            channel._send_sync(outbound)

        send_msg.assert_not_called()
        patch_msg.assert_not_called()
        self.assertEqual(outbound.metadata["delivery"]["status"], "suppressed")
        self.assertEqual(outbound.metadata["delivery"]["reason"], "routine_tool_lifecycle")

    def test_send_step_cards_patches_visible_state_even_when_finish_is_routine(self) -> None:
        channel = FeishuChannel(bus=MessageBus(), app_id="app-id", app_secret="app-secret")
        channel._client = object()
        waiting_metadata = {
            "_event_class": "step_update",
            "_feedback_origin": "adk_plugin",
            "_step_kind": "tool",
            "_step_update_kind": "progress",
            "_step_phase": "waiting",
            "_step_title": "web_search",
            "_step_id": "step-1",
        }
        finished_metadata = {
            **waiting_metadata,
            "_step_update_kind": "lifecycle",
            "_step_phase": "finished",
            "_done": True,
        }
        outbound = OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content="`web_search` is running in the background",
            metadata=waiting_metadata,
        )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(channel, "_send_message_request_sync", return_value="om_step_1") as send_msg,
        ):
            first = channel._send_step_cards_sync(outbound, outbound.content, waiting_metadata)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(channel, "_patch_message_sync") as patch_msg,
        ):
            second = channel._send_step_cards_sync(outbound, "Finished `web_search`", finished_metadata)

        self.assertEqual(first, "om_step_1")
        self.assertEqual(second, "om_step_1")
        send_msg.assert_called_once()
        patch_msg.assert_called_once()

    def test_send_sync_debug_env_keeps_routine_adk_tool_lifecycle_cards(self) -> None:
        channel = FeishuChannel(bus=MessageBus(), app_id="app-id", app_secret="app-secret")
        channel._client = object()
        outbound = OutboundMessage(
            channel="feishu",
            chat_id="oc_group_1",
            content="Finished `web_search`",
            metadata={
                "_event_class": "step_update",
                "_feedback_origin": "adk_plugin",
                "_step_kind": "tool",
                "_step_update_kind": "lifecycle",
                "_step_phase": "finished",
                "_step_title": "web_search",
                "_step_id": "step-1",
            },
        )

        with (
            patch.dict("os.environ", {"OPENPPX_FEISHU_DEBUG_CARDS": "1"}),
            patch.object(channel, "_send_message_request_sync", return_value="om_step_1") as send_msg,
        ):
            channel._send_sync(outbound)

        send_msg.assert_called_once()
        self.assertEqual(outbound.metadata["delivery"]["message_ids"], ["om_step_1"])


if __name__ == "__main__":
    unittest.main()
