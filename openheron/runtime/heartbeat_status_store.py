"""Persistent heartbeat status snapshot helpers for CLI/runtime observability."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def heartbeat_status_path(agent_dir: Path) -> Path:
    """Return the heartbeat status snapshot path for one agent."""

    return agent_dir / "runtime" / "heartbeat_status.json"


def _legacy_heartbeat_status_path(workspace: Path) -> Path:
    return workspace / ".openheron" / "heartbeat_status.json"


def read_heartbeat_status_snapshot(agent_dir: Path) -> dict[str, Any] | None:
    """Read heartbeat status snapshot from one agent dir."""

    path = heartbeat_status_path(agent_dir)
    for candidate in (path, _legacy_heartbeat_status_path(agent_dir)):
        if not candidate.exists():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(raw, dict):
            return raw
    return None


def write_heartbeat_status_snapshot(agent_dir: Path, payload: dict[str, Any]) -> None:
    """Persist one heartbeat status snapshot for CLI/status inspection."""

    path = heartbeat_status_path(agent_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")
