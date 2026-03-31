"""Tests for step-event normalization and runtime plugin publishing."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from openpipixia.runtime.step_events import (
    OpenPpxStepEventPlugin,
    classify_outbound_message,
    configure_step_event_publisher,
    normalize_outbound_metadata,
)
from openpipixia.runtime.tool_context import route_context


class StepEventNormalizationTests(unittest.TestCase):
    def test_normalize_legacy_feedback_into_step_update(self) -> None:
        metadata = normalize_outbound_metadata(
            {
                "_feedback_type": "status",
                "_feedback_status": "completed",
                "_tool_name": "write_file",
                "_function_call_id": "fc_1",
                "_done": True,
            }
        )

        self.assertEqual(metadata["_event_class"], "step_update")
        self.assertEqual(metadata["_step_phase"], "finished")
        self.assertEqual(metadata["_step_kind"], "tool")
        self.assertEqual(metadata["_step_id"], "fc_1")
        self.assertEqual(metadata["_step_title"], "write_file")

    def test_classify_stream_delta(self) -> None:
        normalized = classify_outbound_message("hello", {"_stream_delta": True})

        self.assertEqual(normalized.event_class, "stream_delta")
        self.assertTrue(normalized.is_stream)


class StepEventPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        configure_step_event_publisher(None)

    async def test_before_tool_callback_publishes_started_event(self) -> None:
        published = []

        async def publisher(msg):
            published.append(msg)

        configure_step_event_publisher(publisher)
        plugin = OpenPpxStepEventPlugin()
        invocation_context = SimpleNamespace(invocation_id="inv_1")
        tool = SimpleNamespace(name="browser")
        tool_context = SimpleNamespace(invocation_id="inv_1", function_call_id="fc_1")

        await plugin.before_run_callback(invocation_context=invocation_context)
        with route_context("feishu", "oc_123"):
            await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=tool_context)

        self.assertEqual(len(published), 1)
        msg = published[0]
        self.assertEqual(msg.channel, "feishu")
        self.assertEqual(msg.chat_id, "oc_123")
        self.assertEqual(msg.metadata["_event_class"], "step_update")
        self.assertEqual(msg.metadata["_step_phase"], "started")
        self.assertEqual(msg.metadata["_function_call_id"], "fc_1")

    async def test_on_event_callback_marks_long_running_tool_waiting(self) -> None:
        published = []

        async def publisher(msg):
            published.append(msg)

        configure_step_event_publisher(publisher)
        plugin = OpenPpxStepEventPlugin()
        invocation_context = SimpleNamespace(invocation_id="inv_2")
        function_call = SimpleNamespace(id="fc_wait", name="spawn_subagent")
        event = SimpleNamespace(
            long_running_tool_ids={"fc_wait"},
            get_function_calls=lambda: [function_call],
        )

        await plugin.before_run_callback(invocation_context=invocation_context)
        with route_context("local", "default"):
            await plugin.on_event_callback(invocation_context=invocation_context, event=event)

        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].metadata["_step_phase"], "waiting")


if __name__ == "__main__":
    unittest.main()
