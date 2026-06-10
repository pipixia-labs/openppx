"""Tests for deterministic staged summary quality checks."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from openppx.runtime.staged_summary_quality import (
    evaluate_staged_summary_quality,
    extract_long_task_markers,
)
from openppx.runtime.staged_summary_eval import (
    evaluate_staged_summary_eval_file,
    summarize_staged_summary_quality_log,
)


class StagedSummaryQualityTests(unittest.TestCase):
    def test_extracts_long_task_markers(self) -> None:
        markers = extract_long_task_markers(
            "Task task_123 wrote artifact_report-1 and checkpoint_ckpt-2."
        )

        self.assertEqual(markers, ("artifact_report-1", "checkpoint_ckpt-2", "task_123"))

    def test_quality_report_flags_missing_markers_in_strict_mode(self) -> None:
        report = evaluate_staged_summary_quality(
            source_text="Task task_123 wrote artifact_report-1.",
            summary_text="Task completed.",
            max_summary_chars=100,
            require_marker_preservation=True,
        )

        self.assertFalse(report.ok)
        self.assertEqual(report.missing_markers, ("artifact_report-1", "task_123"))
        self.assertFalse(report.empty)

    def test_quality_report_accepts_compact_summary(self) -> None:
        report = evaluate_staged_summary_quality(
            source_text="Task task_123 wrote artifact_report-1." + ("A" * 200),
            summary_text="task_123 wrote artifact_report-1.",
            max_summary_chars=100,
            require_marker_preservation=True,
        )

        self.assertTrue(report.ok)
        self.assertLess(report.compression_ratio, 1.0)

    def test_quality_report_flags_weak_compression_threshold(self) -> None:
        report = evaluate_staged_summary_quality(
            source_text="A" * 1000,
            summary_text="B" * 700,
            max_summary_chars=1000,
            max_compression_ratio=0.5,
        )

        self.assertFalse(report.ok)
        self.assertTrue(report.weak_compression)

    def test_eval_quality_cases_load_and_match_expected_result(self) -> None:
        path = Path(__file__).parent / "eval" / "staged_summary_quality_cases.json"
        payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], 1)
        self.assertGreaterEqual(len(payload["cases"]), 3)
        for case in payload["cases"]:
            report = evaluate_staged_summary_quality(
                source_text=case["source"],
                summary_text=case["summary"],
                max_summary_chars=case["max_summary_chars"],
                max_compression_ratio=case["max_compression_ratio"],
                require_marker_preservation=case["require_marker_preservation"],
            )
            self.assertEqual(report.ok, case["expected_ok"], case["name"])

    def test_eval_report_summarizes_quality_cases(self) -> None:
        path = Path(__file__).parent / "eval" / "staged_summary_quality_cases.json"

        report = evaluate_staged_summary_eval_file(path)
        payload = report.payload()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["case_count"], 3)
        self.assertEqual(payload["failed_count"], 0)
        self.assertTrue(all(item["matched_expected"] for item in payload["results"]))

    def test_quality_log_report_summarizes_jsonl_observability_events(self) -> None:
        with self.subTest("existing log"):
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "summary-quality.jsonl"
                path.write_text(
                    "\n".join(
                        [
                            json.dumps({"outcome": "accepted", "reason": "ok"}),
                            json.dumps({"outcome": "rejected", "reason": "missing_markers"}),
                            "not-json",
                        ]
                    ),
                    encoding="utf-8",
                )

                report = summarize_staged_summary_quality_log(path, recent_limit=2)

            self.assertTrue(report["ok"])
            self.assertEqual(report["total"], 3)
            self.assertEqual(report["outcomes"]["accepted"], 1)
            self.assertEqual(report["outcomes"]["invalid"], 1)
            self.assertEqual(report["reasons"]["missing_markers"], 1)
            self.assertEqual(len(report["recent_failures"]), 2)


if __name__ == "__main__":
    unittest.main()
