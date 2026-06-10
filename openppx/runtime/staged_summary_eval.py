"""Offline eval/report helpers for staged long-task summaries."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .staged_summary_quality import StagedSummaryQualityReport, evaluate_staged_summary_quality


@dataclass(frozen=True, slots=True)
class StagedSummaryEvalCaseResult:
    """Evaluation result for one staged summary quality case."""

    name: str
    ok: bool
    expected_ok: bool
    matched_expected: bool
    quality: StagedSummaryQualityReport
    missing_required_terms: tuple[str, ...] = field(default_factory=tuple)
    present_forbidden_terms: tuple[str, ...] = field(default_factory=tuple)

    def payload(self) -> dict[str, Any]:
        """Return a JSON-serializable result payload."""
        return {
            "name": self.name,
            "ok": self.ok,
            "expected_ok": self.expected_ok,
            "matched_expected": self.matched_expected,
            "quality": self.quality.payload(),
            "missing_required_terms": list(self.missing_required_terms),
            "present_forbidden_terms": list(self.present_forbidden_terms),
        }


@dataclass(frozen=True, slots=True)
class StagedSummaryEvalReport:
    """Aggregated staged summary eval report."""

    ok: bool
    case_count: int
    passed_count: int
    failed_count: int
    results: tuple[StagedSummaryEvalCaseResult, ...]

    def payload(self) -> dict[str, Any]:
        """Return a JSON-serializable report payload."""
        return {
            "ok": self.ok,
            "case_count": self.case_count,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "results": [result.payload() for result in self.results],
        }


def evaluate_staged_summary_eval_file(path: str | Path) -> StagedSummaryEvalReport:
    """Load and evaluate staged summary cases from a JSON file."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return evaluate_staged_summary_eval_payload(payload)


def evaluate_staged_summary_eval_payload(payload: dict[str, Any]) -> StagedSummaryEvalReport:
    """Evaluate staged summary quality cases from a parsed payload."""
    if not isinstance(payload, dict):
        raise ValueError("staged summary eval payload must be a JSON object")
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("staged summary eval payload must define a cases array")
    results = tuple(_evaluate_case(case, index=index) for index, case in enumerate(cases))
    passed = sum(1 for result in results if result.matched_expected)
    failed = len(results) - passed
    return StagedSummaryEvalReport(
        ok=failed == 0,
        case_count=len(results),
        passed_count=passed,
        failed_count=failed,
        results=results,
    )


def summarize_staged_summary_quality_log(path: str | Path, *, recent_limit: int = 20) -> dict[str, Any]:
    """Summarize JSONL quality events emitted by ``OpenPpxStagedEventsSummarizer``."""
    log_path = Path(path)
    outcomes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    total = 0
    recent_failures: list[dict[str, Any]] = []
    if not log_path.exists():
        return {
            "ok": False,
            "error": "quality log file not found",
            "path": str(log_path),
            "total": 0,
            "outcomes": {},
            "reasons": {},
            "acceptance_rate": 0.0,
            "recent_failures": [],
        }
    with log_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                event = {"outcome": "invalid", "reason": "invalid_json", "line_number": line_number}
            outcome = str(event.get("outcome") or "unknown")
            reason = str(event.get("reason") or "")
            outcomes[outcome] += 1
            if reason:
                reasons[reason] += 1
            total += 1
            if outcome != "accepted":
                recent_failures.append({"line_number": line_number, **event})
                if len(recent_failures) > max(0, recent_limit):
                    recent_failures.pop(0)
    accepted = outcomes.get("accepted", 0)
    return {
        "ok": True,
        "path": str(log_path),
        "total": total,
        "outcomes": dict(sorted(outcomes.items())),
        "reasons": dict(sorted(reasons.items())),
        "acceptance_rate": accepted / total if total else 0.0,
        "recent_failures": recent_failures,
    }


def _evaluate_case(case: Any, *, index: int) -> StagedSummaryEvalCaseResult:
    if not isinstance(case, dict):
        raise ValueError(f"staged summary eval case #{index + 1} must be a JSON object")
    name = str(case.get("name") or f"case-{index + 1}")
    quality = evaluate_staged_summary_quality(
        source_text=str(case.get("source") or ""),
        summary_text=str(case.get("summary") or ""),
        max_summary_chars=_int_or_default(case.get("max_summary_chars"), 0),
        max_compression_ratio=_float_or_default(case.get("max_compression_ratio"), 1.0),
        require_marker_preservation=bool(case.get("require_marker_preservation", False)),
    )
    summary = str(case.get("summary") or "")
    missing_required = tuple(term for term in _string_list(case.get("must_include")) if term not in summary)
    present_forbidden = tuple(term for term in _string_list(case.get("must_not_include")) if term in summary)
    ok = quality.ok and not missing_required and not present_forbidden
    expected_ok = bool(case.get("expected_ok", ok))
    return StagedSummaryEvalCaseResult(
        name=name,
        ok=ok,
        expected_ok=expected_ok,
        matched_expected=ok == expected_ok,
        quality=quality,
        missing_required_terms=missing_required,
        present_forbidden_terms=present_forbidden,
    )


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item))


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


__all__ = [
    "StagedSummaryEvalCaseResult",
    "StagedSummaryEvalReport",
    "evaluate_staged_summary_eval_file",
    "evaluate_staged_summary_eval_payload",
    "summarize_staged_summary_quality_log",
]
