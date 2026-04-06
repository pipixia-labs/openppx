"""Persistent heartbeat status snapshot helpers for CLI/runtime observability."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def heartbeat_status_path(workspace: Path) -> Path:
    """Return the heartbeat status snapshot path for a workspace."""

    return workspace / ".openppx" / "heartbeat_status.json"


def read_heartbeat_status_snapshot(workspace: Path) -> dict[str, Any] | None:
    """Read heartbeat status snapshot from workspace; return None when unavailable."""

    path = heartbeat_status_path(workspace)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def write_heartbeat_status_snapshot(workspace: Path, payload: dict[str, Any]) -> None:
    """Persist one heartbeat status snapshot for CLI/status inspection."""

    path = heartbeat_status_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")
