"""Persistent route-stats snapshot helpers for gateway/runtime observability."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def route_stats_path(workspace: Path) -> Path:
    """Return the route stats snapshot path for one agent dir."""

    return workspace / "runtime" / "route_stats.json"


def _legacy_route_stats_path(workspace: Path) -> Path:
    return workspace / ".openheron" / "route_stats.json"


def read_route_stats_snapshot(workspace: Path) -> dict[str, Any] | None:
    """Read route stats snapshot from one agent dir; fallback to legacy path."""

    for candidate in (route_stats_path(workspace), _legacy_route_stats_path(workspace)):
        if not candidate.exists():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(raw, dict):
            return raw
    return None


def write_route_stats_snapshot(workspace: Path, payload: dict[str, Any]) -> None:
    """Persist one route stats snapshot for CLI/status inspection."""

    path = route_stats_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")
