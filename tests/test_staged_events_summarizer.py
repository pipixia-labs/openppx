"""Tests for openppx ADK staged event summarizer."""

from __future__ import annotations

import asyncio
import json
import tempfile
import types as pytypes
import unittest
from pathlib import Path

from google.adk.events.event import Event
from google.genai import types

import openppx.runtime.staged_events_summarizer as staged_module
from openppx.runtime.staged_events_summarizer import OpenPpxStagedEventsSummarizer


class _FakeLlm:
    def __init__(self, summary: str) -> None:
        self.model = "fake-summary-model"
        self.summary = summary

    async def generate_content_async(self, llm_request, stream: bool = False):
        _ = llm_request, stream
        yield pytypes.SimpleNamespace(
            content=types.Content(role="model", parts=[types.Part(text=self.summary)]),
            usage_metadata=None,
        )


def _event(text: str, *, author: str = "user") -> Event:
    return Event(
        author=author,
        content=types.Content(role=author, parts=[types.Part(text=text)]),
    )


class StagedEventsSummarizerTests(unittest.TestCase):
    def test_accepts_compact_non_empty_summary(self) -> None:
        events = [
            _event("用户目标：完成长任务 runtime。" + "A" * 500),
            _event("TaskRun task_1 completed with artifact output." + "B" * 500, author="model"),
        ]
        summarizer = OpenPpxStagedEventsSummarizer(
            llm=_FakeLlm("Goal: 完成长任务 runtime.\nTaskRuns: task_1 completed."),
            min_source_chars=1,
        )

        compacted = asyncio.run(summarizer.maybe_summarize_events(events=events))

        self.assertIsNotNone(compacted)
        assert compacted is not None
        self.assertIsNotNone(compacted.actions.compaction)
        text = compacted.actions.compaction.compacted_content.parts[0].text  # type: ignore[union-attr]
        self.assertIn("task_1 completed", text)

    def test_rejects_empty_summary(self) -> None:
        summarizer = OpenPpxStagedEventsSummarizer(llm=_FakeLlm("   "), min_source_chars=1)

        compacted = asyncio.run(summarizer.maybe_summarize_events(events=[_event("A" * 500)]))

        self.assertIsNone(compacted)

    def test_rejects_inflated_summary(self) -> None:
        summarizer = OpenPpxStagedEventsSummarizer(llm=_FakeLlm("B" * 600), min_source_chars=1)

        compacted = asyncio.run(summarizer.maybe_summarize_events(events=[_event("A" * 200)]))

        self.assertIsNone(compacted)

    def test_truncates_summary_to_configured_max(self) -> None:
        summarizer = OpenPpxStagedEventsSummarizer(
            llm=_FakeLlm("TaskRuns: " + ("B" * 200)),
            max_summary_chars=20,
            min_source_chars=0,
        )

        compacted = asyncio.run(summarizer.maybe_summarize_events(events=[_event("A" * 500)]))

        self.assertIsNotNone(compacted)
        assert compacted is not None
        text = compacted.actions.compaction.compacted_content.parts[0].text  # type: ignore[union-attr]
        self.assertLessEqual(len(text), 20)

    def test_public_exports_include_configuration_constants(self) -> None:
        self.assertIn("DEFAULT_STAGED_SUMMARY_MAX_CHARS", staged_module.__all__)
        self.assertIn("DEFAULT_STAGED_SUMMARY_MIN_SOURCE_CHARS", staged_module.__all__)

    def test_strict_marker_mode_rejects_summary_that_drops_task_id(self) -> None:
        summarizer = OpenPpxStagedEventsSummarizer(
            llm=_FakeLlm("Goal: continue the long task."),
            min_source_chars=1,
            require_marker_preservation=True,
        )

        compacted = asyncio.run(
            summarizer.maybe_summarize_events(events=[_event("TaskRun task_123 is running." + "A" * 500)])
        )

        self.assertIsNone(compacted)
        self.assertIsNotNone(summarizer.last_quality_report)
        assert summarizer.last_quality_report is not None
        self.assertEqual(summarizer.last_quality_report.missing_markers, ("task_123",))

    def test_rejects_summary_above_configured_compression_ratio(self) -> None:
        summarizer = OpenPpxStagedEventsSummarizer(
            llm=_FakeLlm("B" * 80),
            min_source_chars=1,
            max_compression_ratio=0.5,
        )

        compacted = asyncio.run(summarizer.maybe_summarize_events(events=[_event("A" * 100)]))

        self.assertIsNone(compacted)
        self.assertIsNotNone(summarizer.last_quality_report)
        assert summarizer.last_quality_report is not None
        self.assertTrue(summarizer.last_quality_report.weak_compression)

    def test_writes_optional_quality_log_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "quality.jsonl"
            summarizer = OpenPpxStagedEventsSummarizer(
                llm=_FakeLlm("TaskRuns: task_1 completed."),
                min_source_chars=1,
                quality_log_path=str(log_path),
            )

            compacted = asyncio.run(summarizer.maybe_summarize_events(events=[_event("task_1 " + ("A" * 500))]))

            self.assertIsNotNone(compacted)
            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["outcome"], "accepted")
            self.assertEqual(rows[-1]["quality"]["missing_markers"], [])


if __name__ == "__main__":
    unittest.main()
