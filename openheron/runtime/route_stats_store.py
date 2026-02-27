"""Persistent route-stats snapshot helpers for gateway/runtime observability."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def route_stats_path(workspace: Path) -> Path:
    """Return the route stats snapshot path for a workspace."""

    return workspace / ".openheron" / "route_stats.json"


def read_route_stats_snapshot(workspace: Path) -> dict[str, Any] | None:
    """Read route stats snapshot from workspace; return None when unavailable."""

    path = route_stats_path(workspace)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def write_route_stats_snapshot(workspace: Path, payload: dict[str, Any]) -> None:
    """Persist one route stats snapshot for CLI/status inspection."""

    path = route_stats_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")
