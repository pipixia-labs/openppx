"""Deterministic quality checks for staged long-task summaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_MARKER_PATTERN = re.compile(
    r"\b(?:task|flow|checkpoint|ckpt|artifact)[_-][A-Za-z0-9_:-]+\b"
)


@dataclass(frozen=True, slots=True)
class StagedSummaryQualityReport:
    """Quality report for one candidate staged summary."""

    ok: bool
    source_chars: int
    summary_chars: int
    compression_ratio: float
    empty: bool
    inflated: bool
    over_budget: bool
    weak_compression: bool
    missing_markers: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        """Return a JSON-serializable quality report."""
        return {
            "ok": self.ok,
            "source_chars": self.source_chars,
            "summary_chars": self.summary_chars,
            "compression_ratio": self.compression_ratio,
            "empty": self.empty,
            "inflated": self.inflated,
            "over_budget": self.over_budget,
            "weak_compression": self.weak_compression,
            "missing_markers": list(self.missing_markers),
        }


def extract_long_task_markers(text: str, *, limit: int = 100) -> tuple[str, ...]:
    """Return stable task/checkpoint/artifact markers that should survive strict summary mode."""
    markers = sorted(set(_MARKER_PATTERN.findall(text or "")))
    return tuple(markers[: max(0, limit)])


def evaluate_staged_summary_quality(
    *,
    source_text: str,
    summary_text: str,
    max_summary_chars: int,
    max_compression_ratio: float = 1.0,
    require_marker_preservation: bool = False,
) -> StagedSummaryQualityReport:
    """Evaluate deterministic quality gates for one staged summary."""
    source = source_text or ""
    summary = (summary_text or "").strip()
    source_chars = len(source)
    summary_chars = len(summary)
    empty = not summary
    inflated = source_chars > 0 and summary_chars >= source_chars
    over_budget = max_summary_chars > 0 and summary_chars > max_summary_chars
    compression_ratio = (summary_chars / source_chars) if source_chars else 0.0
    weak_compression = (
        source_chars > 0
        and max_compression_ratio > 0
        and compression_ratio > max_compression_ratio
    )
    missing_markers: tuple[str, ...] = ()
    if require_marker_preservation:
        missing_markers = tuple(
            marker for marker in extract_long_task_markers(source) if marker not in summary
        )
    ok = not empty and not inflated and not over_budget and not weak_compression and not missing_markers
    return StagedSummaryQualityReport(
        ok=ok,
        source_chars=source_chars,
        summary_chars=summary_chars,
        compression_ratio=compression_ratio,
        empty=empty,
        inflated=inflated,
        over_budget=over_budget,
        weak_compression=weak_compression,
        missing_markers=missing_markers,
    )


__all__ = [
    "StagedSummaryQualityReport",
    "evaluate_staged_summary_quality",
    "extract_long_task_markers",
]
