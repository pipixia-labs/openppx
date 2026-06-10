"""ADK-native staged event summarizer for long-task context compaction."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from google.adk.apps.base_events_summarizer import BaseEventsSummarizer
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.events.event import Event

from .staged_summary_quality import StagedSummaryQualityReport, evaluate_staged_summary_quality


DEFAULT_STAGED_SUMMARY_MAX_CHARS = 4_000
DEFAULT_STAGED_SUMMARY_MIN_SOURCE_CHARS = 400

OPENPPX_STAGED_SUMMARY_PROMPT = """You are compacting openppx long-task conversation events.

Return a concise, factual summary with these layers when present:
- Goal: user objective, success criteria, unresolved decisions.
- TaskRuns: task_id, status, important output/artifact/checkpoint references.
- TaskFlow: current step, ready/blocked steps, dependencies, evidence.
- Context: durable facts, files changed, commands/tests run, next safe action.

Rules:
- Preserve ids, file paths, task states, checkpoint refs, and explicit user constraints.
- Do not claim a task completed unless the events explicitly prove it.
- Do not include generic advice or conversational filler.

Conversation events:

{conversation_history}
"""


class OpenPpxStagedEventsSummarizer(BaseEventsSummarizer):
    """LLM event summarizer with openppx-specific validation and fallback.

    ADK decides when compaction should run. This class only controls how the
    selected events are summarized and whether the result is safe to accept.
    Returning ``None`` tells ADK that compaction failed and the original events
    should be retained.
    """

    def __init__(
        self,
        *,
        llm: Any,
        prompt_template: str | None = None,
        max_summary_chars: int = DEFAULT_STAGED_SUMMARY_MAX_CHARS,
        min_source_chars: int = DEFAULT_STAGED_SUMMARY_MIN_SOURCE_CHARS,
        max_compression_ratio: float = 1.0,
        require_marker_preservation: bool = False,
        quality_log_path: str | None = None,
    ) -> None:
        self._inner = LlmEventSummarizer(
            llm=llm,
            prompt_template=prompt_template or OPENPPX_STAGED_SUMMARY_PROMPT,
        )
        self.max_summary_chars = max(1, int(max_summary_chars or DEFAULT_STAGED_SUMMARY_MAX_CHARS))
        self.min_source_chars = max(0, int(min_source_chars))
        self.max_compression_ratio = max(0.0, float(max_compression_ratio or 0.0))
        self.require_marker_preservation = bool(require_marker_preservation)
        self.quality_log_path = str(quality_log_path or "").strip()
        self.last_quality_report: StagedSummaryQualityReport | None = None

    async def maybe_summarize_events(self, *, events: list[Event]) -> Event | None:
        """Return a validated ADK compaction event, or ``None`` on failure."""
        self.last_quality_report = None
        if not events:
            return None
        source_text = _events_text(events)
        source_chars = len(source_text)
        if source_chars < self.min_source_chars:
            self._record_quality_event(
                outcome="skipped",
                reason="source_below_min_chars",
                report=None,
                event_count=len(events),
            )
            return None
        compacted = await self._inner.maybe_summarize_events(events=events)
        if compacted is None:
            self._record_quality_event(
                outcome="rejected",
                reason="inner_summarizer_returned_none",
                report=None,
                event_count=len(events),
            )
            return None
        summary_text = _compaction_event_text(compacted).strip()
        quality = evaluate_staged_summary_quality(
            source_text=source_text,
            summary_text=summary_text,
            max_summary_chars=0,
            max_compression_ratio=self.max_compression_ratio,
            require_marker_preservation=self.require_marker_preservation,
        )
        self.last_quality_report = quality
        if quality.empty or quality.inflated or quality.weak_compression or quality.missing_markers:
            self._record_quality_event(
                outcome="rejected",
                reason=_quality_rejection_reason(quality),
                report=quality,
                event_count=len(events),
            )
            return None
        if len(summary_text) > self.max_summary_chars:
            _truncate_compaction_event_text(compacted, max_chars=self.max_summary_chars)
            summary_text = _compaction_event_text(compacted).strip()
            quality = evaluate_staged_summary_quality(
                source_text=source_text,
                summary_text=summary_text,
                max_summary_chars=self.max_summary_chars,
                max_compression_ratio=self.max_compression_ratio,
                require_marker_preservation=self.require_marker_preservation,
            )
            self.last_quality_report = quality
            if not quality.ok:
                self._record_quality_event(
                    outcome="rejected",
                    reason=_quality_rejection_reason(quality),
                    report=quality,
                    event_count=len(events),
                )
                return None
            self._record_quality_event(
                outcome="accepted",
                reason="truncated",
                report=quality,
                event_count=len(events),
            )
            return compacted
        self._record_quality_event(
            outcome="accepted",
            reason="ok",
            report=quality,
            event_count=len(events),
        )
        return compacted

    def _record_quality_event(
        self,
        *,
        outcome: str,
        reason: str,
        report: StagedSummaryQualityReport | None,
        event_count: int,
    ) -> None:
        """Append an optional JSONL quality event for staged summary observability."""
        if not self.quality_log_path:
            return
        payload: dict[str, Any] = {
            "outcome": outcome,
            "reason": reason,
            "event_count": event_count,
            "require_marker_preservation": self.require_marker_preservation,
            "max_summary_chars": self.max_summary_chars,
            "min_source_chars": self.min_source_chars,
            "max_compression_ratio": self.max_compression_ratio,
        }
        if report is not None:
            payload["quality"] = report.payload()
        try:
            path = Path(os.path.expanduser(self.quality_log_path))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            return


def _events_text(events: list[Event]) -> str:
    """Return concatenated text represented by events."""
    chunks: list[str] = []
    for event in events:
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                chunks.append(str(text))
    return "\n".join(chunks)


def _events_text_chars(events: list[Event]) -> int:
    """Return the text character count represented by events."""
    return len(_events_text(events))


def _compaction_event_text(event: Event) -> str:
    """Extract text from an ADK compaction event."""
    compaction = getattr(getattr(event, "actions", None), "compaction", None)
    content = getattr(compaction, "compacted_content", None)
    parts = getattr(content, "parts", None) or []
    return "\n".join(str(part.text) for part in parts if getattr(part, "text", None))


def _truncate_compaction_event_text(event: Event, *, max_chars: int) -> None:
    """Trim compaction text in place while preserving ADK event structure."""
    compaction = getattr(getattr(event, "actions", None), "compaction", None)
    content = getattr(compaction, "compacted_content", None)
    parts = getattr(content, "parts", None) or []
    remaining = max(0, max_chars)
    for part in parts:
        text = getattr(part, "text", None)
        if not text:
            continue
        if remaining <= 0:
            part.text = ""
            continue
        normalized = str(text)
        if len(normalized) > remaining:
            suffix = "..." if remaining >= 3 else "." * remaining
            prefix = normalized[: max(0, remaining - len(suffix))].rstrip()
            part.text = (prefix + suffix)[:remaining]
            remaining = 0
        else:
            remaining -= len(normalized)


def _quality_rejection_reason(report: StagedSummaryQualityReport) -> str:
    """Return a compact primary rejection reason for one quality report."""
    if report.empty:
        return "empty"
    if report.inflated:
        return "inflated"
    if report.weak_compression:
        return "weak_compression"
    if report.over_budget:
        return "over_budget"
    if report.missing_markers:
        return "missing_markers"
    return "quality_failed"


__all__ = [
    "DEFAULT_STAGED_SUMMARY_MAX_CHARS",
    "DEFAULT_STAGED_SUMMARY_MIN_SOURCE_CHARS",
    "OPENPPX_STAGED_SUMMARY_PROMPT",
    "OpenPpxStagedEventsSummarizer",
]
