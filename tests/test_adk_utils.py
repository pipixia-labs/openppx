"""Tests for ADK text extraction and stream merging helpers."""

from __future__ import annotations

import asyncio
import types as pytypes
import unittest

from openppx.runtime.adk_utils import extract_text, merge_text_stream, run_text_async


class AdkUtilsTests(unittest.TestCase):
    def test_extract_text_preserves_text_part_spacing(self) -> None:
        content = pytypes.SimpleNamespace(
            parts=[
                pytypes.SimpleNamespace(text="hello"),
                pytypes.SimpleNamespace(text=" world"),
                pytypes.SimpleNamespace(text=None),
            ]
        )

        self.assertEqual(extract_text(content), "hello world")

    def test_extract_text_skips_thought_parts(self) -> None:
        content = pytypes.SimpleNamespace(
            parts=[
                pytypes.SimpleNamespace(text="internal reasoning", thought=True),
                pytypes.SimpleNamespace(text="final answer", thought=False),
            ]
        )

        self.assertEqual(extract_text(content), "final answer")

    def test_merge_text_stream_appends_delta_chunks_without_newline(self) -> None:
        merged = merge_text_stream("", "hello")
        merged = merge_text_stream(merged, " world")

        self.assertEqual(merged, "hello world")

    def test_merge_text_stream_keeps_snapshot_updates(self) -> None:
        merged = merge_text_stream("", "hello")
        merged = merge_text_stream(merged, "hello world")

        self.assertEqual(merged, "hello world")

    def test_merge_text_stream_skips_final_aggregate_after_deltas(self) -> None:
        merged = merge_text_stream("", "hello")
        merged = merge_text_stream(merged, " world")
        merged = merge_text_stream(merged, "hello world")

        self.assertEqual(merged, "hello world")

    def test_run_text_async_merges_events_and_reports_updates(self) -> None:
        events = [
            pytypes.SimpleNamespace(content=pytypes.SimpleNamespace(parts=[])),
            pytypes.SimpleNamespace(
                content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="hello")])
            ),
            pytypes.SimpleNamespace(
                content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text=" world")])
            ),
            pytypes.SimpleNamespace(
                content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="hello world")])
            ),
        ]
        observed_events: list[object] = []
        updates: list[tuple[str, str]] = []

        class _FakeRunner:
            async def run_async(self, **kwargs):
                self.kwargs = kwargs
                for event in events:
                    yield event

        runner = _FakeRunner()

        async def _run() -> str:
            return await run_text_async(
                runner,
                on_event=observed_events.append,
                on_text_update=lambda merged, delta: updates.append((merged, delta)),
                user_id="u1",
                session_id="s1",
            )

        final = asyncio.run(_run())

        self.assertEqual(final, "hello world")
        self.assertEqual(observed_events, events)
        self.assertEqual(updates, [("hello", "hello"), ("hello world", " world")])
        self.assertEqual(runner.kwargs["user_id"], "u1")
        self.assertEqual(runner.kwargs["session_id"], "s1")

    def test_run_text_async_returns_default_when_empty(self) -> None:
        class _FakeRunner:
            async def run_async(self, **kwargs):
                if False:
                    yield None

        final = asyncio.run(run_text_async(_FakeRunner(), default_when_empty="(empty)"))

        self.assertEqual(final, "(empty)")


if __name__ == "__main__":
    unittest.main()
