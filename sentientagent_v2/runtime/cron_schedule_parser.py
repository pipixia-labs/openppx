"""Shared parsing helpers for cron schedule arguments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .cron_service import CronSchedule


@dataclass(slots=True, frozen=True)
class ParsedCronSchedule:
    """Normalized schedule parsing result."""

    schedule: CronSchedule
    delete_after_run: bool


def parse_schedule_input(
    *,
    every_seconds: int | None,
    cron_expr: str | None,
    at: str | None,
    tz: str | None,
) -> tuple[ParsedCronSchedule | None, str | None]:
    """Parse one schedule source into a normalized cron schedule.

    Returns:
        (ParsedCronSchedule, None) on success, otherwise (None, error message).
    """
    normalized_cron_expr = (cron_expr or "").strip() or None
    normalized_at = (at or "").strip() or None
    normalized_tz = (tz or "").strip() or None
    mode_count = int(every_seconds is not None) + int(normalized_cron_expr is not None) + int(normalized_at is not None)
    if mode_count != 1:
        return None, "provide exactly one schedule source: every_seconds OR cron_expr OR at"

    if every_seconds is not None:
        if every_seconds <= 0:
            return None, "every_seconds must be > 0"
        return ParsedCronSchedule(
            schedule=CronSchedule(kind="every", every_seconds=every_seconds),
            delete_after_run=False,
        ), None

    if normalized_cron_expr is not None:
        if normalized_tz:
            try:
                ZoneInfo(normalized_tz)
            except Exception:
                return None, f"unknown timezone '{normalized_tz}'"
        return ParsedCronSchedule(
            schedule=CronSchedule(kind="cron", cron_expr=normalized_cron_expr, tz=normalized_tz),
            delete_after_run=False,
        ), None

    # Remaining branch is one-shot absolute execution.
    if normalized_at is None:
        return None, "at must not be empty"
    try:
        at_ms = int(datetime.fromisoformat(normalized_at).timestamp() * 1000)
    except ValueError:
        return None, "at must be a valid ISO datetime string"
    return ParsedCronSchedule(
        schedule=CronSchedule(kind="at", at_ms=at_ms),
        delete_after_run=True,
    ), None
