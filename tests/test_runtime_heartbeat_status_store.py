"""Tests for persistent heartbeat status snapshot helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpipixia.runtime.heartbeat_status_store import (
    heartbeat_status_path,
    read_heartbeat_status_snapshot,
    write_heartbeat_status_snapshot,
)


class HeartbeatStatusStoreTests(unittest.TestCase):
    def test_roundtrip_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            payload = {"running": True, "recent_reason_counts": {"cron": 2}}
            write_heartbeat_status_snapshot(workspace, payload)
            loaded = read_heartbeat_status_snapshot(workspace)

        self.assertEqual(loaded, payload)

    def test_path_is_under_openpipixia_runtime_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            path = heartbeat_status_path(workspace)
        self.assertEqual(path, workspace / ".openppx" / "heartbeat_status.json")

    def test_read_returns_none_when_payload_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            path = heartbeat_status_path(workspace)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[]", encoding="utf-8")
            loaded = read_heartbeat_status_snapshot(workspace)
        self.assertIsNone(loaded)


if __name__ == "__main__":
    unittest.main()
