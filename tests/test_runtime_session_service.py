"""Tests for SQLite session service factory."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openheron.runtime.session_service import (
    SessionConfig,
    create_session_service,
    load_session_config,
)


class SessionServiceFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_load_defaults_to_home_database_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}, clear=False):
                os.environ.pop("OPENHERON_SESSION_DB_URL", None)
                cfg = load_session_config()
                self.assertTrue(cfg.db_url.startswith("sqlite+aiosqlite:///"))
                self.assertIn(".openheron/agents/main/sessions/sessions.db", cfg.db_url)
                db_file = Path(cfg.db_url.replace("sqlite+aiosqlite:///", "", 1))
                self.assertTrue(str(db_file.parent).endswith("/.openheron/agents/main/sessions"))

    def test_load_uses_explicit_db_url_when_set(self) -> None:
        db_url = "sqlite+aiosqlite:////tmp/custom.db"
        os.environ["OPENHERON_SESSION_DB_URL"] = db_url
        cfg = load_session_config()
        self.assertEqual(cfg.db_url, db_url)

    def test_create_sqlite_backend_uses_db_url(self) -> None:
        db_url = "sqlite+aiosqlite:////tmp/sessions.db"
        with patch("openheron.runtime.session_service.DatabaseSessionService") as mocked:
            mocked.return_value = object()
            out = create_session_service(SessionConfig(db_url=db_url))
            self.assertIsNotNone(out)
            mocked.assert_called_once_with(db_url)


if __name__ == "__main__":
    unittest.main()
