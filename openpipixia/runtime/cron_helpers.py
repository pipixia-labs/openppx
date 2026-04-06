"""Shared formatting/path helpers for cron-related CLI and tools code."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any


def cron_store_path(workspace_root: Path) -> Path:
    """Return the canonical cron store file under a workspace root."""
    return workspace_root / ".openppx" / "cron_jobs.json"


def format_schedule(schedule: Any) -> str:
    """Format a cron schedule object into a stable human-readable string."""
    if schedule is None:
        return "unknown"

    kind = getattr(schedule, "kind", "") or ""
    if kind == "every":
        return f"every:{getattr(schedule, 'every_seconds', 0)}s"
    if kind == "cron":
        expr = getattr(schedule, "cron_expr", "") or ""
        tz = getattr(schedule, "tz", None)
        return f"cron:{expr} ({tz})" if tz else f"cron:{expr}"
    if kind == "at":
        at_ms = getattr(schedule, "at_ms", None)
        if at_ms:
            return f"at:{dt.datetime.fromtimestamp(at_ms / 1000).isoformat(timespec='seconds')}"
        return "at:unknown"
    return str(kind or "unknown")


def format_timestamp_ms(ms: int | None) -> str:
    """Format Unix milliseconds as ISO local time (or '-' for None)."""
    if ms is None:
        return "-"
    return dt.datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")
