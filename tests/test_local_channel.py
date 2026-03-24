"""Tests for local channel rendering."""

from __future__ import annotations

import os
import unittest

from openpipixia.bus.events import OutboundMessage
from openpipixia.bus.queue import MessageBus
from openpipixia.channels.local import LocalChannel


class LocalChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    async def test_send_renders_plain_text_by_default(self) -> None:
        lines: list[str] = []
        channel = LocalChannel(bus=MessageBus(), writer=lines.append)

        await channel.send(OutboundMessage(channel="local", chat_id="terminal", content="hello"))

        self.assertEqual(lines, ["hello"])

    async def test_send_renders_feedback_status_block(self) -> None:
        lines: list[str] = []
        channel = LocalChannel(bus=MessageBus(), writer=lines.append)

        await channel.send(
            OutboundMessage(
                channel="local",
                chat_id="terminal",
                content="Command still running (session abc).",
                metadata={
                    "_feedback_type": "status",
                    "_feedback_status": "running",
                    "_tool_name": "exec",
                    "_step_title": "Command still running",
                    "_session_id": "abc",
                },
            )
        )

        self.assertEqual(len(lines), 1)
        self.assertIn("[status:running]", lines[0])
        self.assertIn("Command still running - exec", lines[0])
        self.assertIn("session=abc", lines[0])

    async def test_send_renders_tool_output_block(self) -> None:
        lines: list[str] = []
        channel = LocalChannel(bus=MessageBus(), writer=lines.append)

        await channel.send(
            OutboundMessage(
                channel="local",
                chat_id="terminal",
                content="line-1\nline-2",
                metadata={
                    "_feedback_type": "tool_output",
                    "_tool_name": "process",
                    "_step_title": "Process output",
                    "_session_id": "abc",
                },
            )
        )

        self.assertEqual(len(lines), 1)
        self.assertIn("[output]", lines[0])
        self.assertIn("Process output - process", lines[0])
        self.assertIn("    line-1", lines[0])
        self.assertIn("    line-2", lines[0])

    async def test_send_can_fallback_to_json_output(self) -> None:
        os.environ["OPENPIPIXIA_LOCAL_JSON_OUTPUT"] = "1"
        lines: list[str] = []
        channel = LocalChannel(bus=MessageBus(), writer=lines.append)

        await channel.send(OutboundMessage(channel="local", chat_id="terminal", content="hello"))

        self.assertEqual(len(lines), 1)
        self.assertIn('"channel": "local"', lines[0])
        self.assertIn('"content": "hello"', lines[0])

    async def test_send_delta_renders_stream_snapshots(self) -> None:
        lines: list[str] = []
        channel = LocalChannel(bus=MessageBus(), writer=lines.append)

        await channel.send_delta("terminal", "hel")
        await channel.send_delta("terminal", "lo")
        await channel.send_delta("terminal", "", {"_stream_end": True})

        self.assertEqual(lines, ["[stream] hel", "[stream] hello"])

    async def test_ingest_text_requests_streaming(self) -> None:
        bus = MessageBus()
        channel = LocalChannel(bus=bus, writer=lambda _line: None)

        await channel.ingest_text("hello", chat_id="terminal", sender_id="u1")

        inbound = await bus.consume_inbound()
        self.assertTrue(inbound.metadata.get("_wants_stream"))


if __name__ == "__main__":
    unittest.main()
