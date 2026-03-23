"""Helpers for injecting per-request time into LLM messages."""

from __future__ import annotations

from datetime import datetime


def _normalize_local(now: datetime | None) -> datetime:
    """Return an aware datetime while preserving provided timezone."""
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz is None:
            return now
        now = now.replace(tzinfo=local_tz)
    return now


def _tz_label(now: datetime) -> str:
    tz = now.tzinfo
    key = getattr(tz, "key", None)
    if isinstance(key, str) and key:
        return key
    return now.tzname() or "local"


def build_current_time_line(now: datetime | None = None, *, label: str = "Current time") -> str:
    """Format a stable current-time line for prompt injection."""
    value = _normalize_local(now)
    return f"{label}: {value.isoformat(timespec='seconds')} ({_tz_label(value)})"


def inject_request_time(message: str, *, received_at: datetime | None = None) -> str:
    """Prepend request receive time to inbound user content."""
    time_line = build_current_time_line(received_at, label="Current request time")
    guidance = "Use this as the reference 'now' for relative time expressions in this message."
    body = message or ""
    if body:
        return f"{time_line}\n{guidance}\n\n{body}"
    return f"{time_line}\n{guidance}"


def append_execution_time(message: str, *, now: datetime | None = None) -> str:
    """Append execution time to cron-triggered payloads."""
    time_line = build_current_time_line(now, label="Current time")
    body = message or ""
    if body:
        return f"{body}\n\n{time_line}"
    return time_line
