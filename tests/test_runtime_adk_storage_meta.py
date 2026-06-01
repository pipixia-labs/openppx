"""Tests for ADK storage sidecar metadata."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openppx.runtime.adk_storage_meta import (
    adk_storage_meta_path,
    ensure_adk_storage_meta,
    ensure_adk_storage_meta_for_sqlite_path,
    infer_data_dir_from_sqlite_path,
    sqlite_path_from_db_url,
)


class AdkStorageMetaTests(unittest.TestCase):
    def test_legacy_data_dir_without_marker_is_stamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "agent"
            (data_dir / "database").mkdir(parents=True)
            (data_dir / "database" / "sessions.db").write_text("", encoding="utf-8")

            with patch("openppx.runtime.adk_storage_meta.installed_adk_version", return_value="2.1.0"):
                meta_path = ensure_adk_storage_meta(data_dir)

            self.assertEqual(meta_path, adk_storage_meta_path(data_dir))
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["adk_major"], 2)
            self.assertEqual(payload["adk_version"], "2.1.0")
            self.assertEqual(payload["last_writer"], "openppx")

    def test_mismatched_adk_major_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "agent"
            meta_path = adk_storage_meta_path(data_dir)
            meta_path.parent.mkdir(parents=True)
            meta_path.write_text(
                json.dumps({"schema_version": 1, "adk_major": 1, "last_writer": "openppx"}),
                encoding="utf-8",
            )

            with patch("openppx.runtime.adk_storage_meta.installed_adk_version", return_value="2.1.0"):
                with self.assertRaisesRegex(RuntimeError, "metadata mismatch"):
                    ensure_adk_storage_meta(data_dir)

    def test_sqlite_url_path_extraction(self) -> None:
        path = sqlite_path_from_db_url("sqlite+aiosqlite:////tmp/openppx/database/sessions.db")

        self.assertEqual(path, Path("/tmp/openppx/database/sessions.db"))
        self.assertIsNone(sqlite_path_from_db_url("postgresql://example"))

    def test_database_parent_path_infers_data_dir(self) -> None:
        db_path = Path("/tmp/openppx/agent/database/memory.db")

        self.assertEqual(infer_data_dir_from_sqlite_path(db_path), Path("/tmp/openppx/agent"))
        self.assertIsNone(infer_data_dir_from_sqlite_path(Path("/tmp/openppx/memory.db")))

    def test_non_openppx_sqlite_path_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "custom.db"

            self.assertIsNone(ensure_adk_storage_meta_for_sqlite_path(db_path))
            self.assertFalse((Path(tmp) / "database" / ".adk_meta.json").exists())


if __name__ == "__main__":
    unittest.main()
