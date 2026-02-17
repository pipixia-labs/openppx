"""Tests for persistent config helpers."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from sentientagent_v2.config import (
    apply_config_to_env,
    bootstrap_env_from_config,
    default_config,
    load_config,
    save_config,
)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_load_missing_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp) / "config.json")
        self.assertTrue(cfg["channels"]["local"]["enabled"])
        self.assertFalse(cfg["channels"]["feishu"]["enabled"])
        self.assertTrue(cfg["providers"]["google"]["enabled"])
        self.assertTrue(cfg["web"]["search"]["enabled"])
        self.assertEqual(cfg["session"]["dbUrl"], "")

    def test_save_then_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["channels"]["local"]["enabled"] = False
            cfg["channels"]["feishu"]["enabled"] = True
            cfg["channels"]["feishu"]["appId"] = "app-id-1"
            cfg["channels"]["feishu"]["appSecret"] = "app-secret-1"
            save_config(cfg, path)
            loaded = load_config(path)

        self.assertFalse(loaded["channels"]["local"]["enabled"])
        self.assertTrue(loaded["channels"]["feishu"]["enabled"])
        self.assertEqual(loaded["channels"]["feishu"]["appId"], "app-id-1")
        self.assertEqual(loaded["channels"]["feishu"]["appSecret"], "app-secret-1")

    def test_apply_config_to_env_respects_existing_values(self) -> None:
        os.environ["SENTIENTAGENT_V2_MODEL"] = "from-shell"
        os.environ["GOOGLE_API_KEY"] = "key-from-shell"
        cfg = default_config()
        cfg["providers"]["google"]["model"] = "from-config"
        cfg["providers"]["google"]["apiKey"] = "key-from-config"
        apply_config_to_env(cfg, overwrite=False)
        self.assertEqual(os.environ["SENTIENTAGENT_V2_MODEL"], "from-shell")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "key-from-shell")

        apply_config_to_env(cfg, overwrite=True)
        self.assertEqual(os.environ["SENTIENTAGENT_V2_MODEL"], "from-config")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "key-from-config")

    def test_bootstrap_env_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["channels"]["local"]["enabled"] = False
            cfg["channels"]["feishu"]["enabled"] = True
            cfg["channels"]["feishu"]["appId"] = "app-id"
            cfg["channels"]["feishu"]["appSecret"] = "app-secret"
            cfg["session"]["dbUrl"] = "sqlite+aiosqlite:////tmp/sessions.db"
            cfg["providers"]["google"]["apiKey"] = "google-key"
            cfg["web"]["search"]["enabled"] = False
            save_config(cfg, path)

            os.environ.pop("SENTIENTAGENT_V2_CHANNELS", None)
            os.environ.pop("FEISHU_APP_ID", None)
            os.environ.pop("SENTIENTAGENT_V2_SESSION_DB_URL", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("BRAVE_API_KEY", None)
            os.environ.pop("SENTIENTAGENT_V2_WEB_SEARCH_ENABLED", None)
            loaded = bootstrap_env_from_config(path)

        self.assertIsNotNone(loaded)
        self.assertEqual(os.environ["SENTIENTAGENT_V2_CHANNELS"], "feishu")
        self.assertEqual(os.environ["FEISHU_APP_ID"], "app-id")
        self.assertEqual(os.environ["SENTIENTAGENT_V2_SESSION_DB_URL"], "sqlite+aiosqlite:////tmp/sessions.db")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "google-key")
        self.assertEqual(os.environ["SENTIENTAGENT_V2_WEB_SEARCH_ENABLED"], "0")
        self.assertNotIn("BRAVE_API_KEY", os.environ)

    def test_bootstrap_env_overwrites_and_clears_managed_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["channels"]["feishu"]["appId"] = ""
            cfg["channels"]["feishu"]["appSecret"] = ""
            cfg["providers"]["google"]["apiKey"] = "from-config"
            save_config(cfg, path)

            os.environ["GOOGLE_API_KEY"] = "from-shell"
            os.environ["FEISHU_APP_ID"] = "stale-feishu-id"
            os.environ["FEISHU_APP_SECRET"] = "stale-feishu-secret"
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["GOOGLE_API_KEY"], "from-config")
        self.assertNotIn("FEISHU_APP_ID", os.environ)
        self.assertNotIn("FEISHU_APP_SECRET", os.environ)

    def test_web_search_api_key_is_loaded_from_web_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["web"]["search"]["apiKey"] = "brave-key"
            save_config(cfg, path)

            os.environ.pop("BRAVE_API_KEY", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["BRAVE_API_KEY"], "brave-key")

    def test_legacy_keys_are_not_used_anymore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["providers"]["google"]["apiKey"] = ""
            cfg["web"]["search"]["apiKey"] = ""
            cfg["keys"] = {"googleApiKey": "legacy-google", "braveApiKey": "legacy-brave"}
            save_config(cfg, path)
            loaded_cfg = load_config(path)

            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("BRAVE_API_KEY", None)
            bootstrap_env_from_config(path)

        self.assertNotIn("keys", loaded_cfg)
        self.assertNotIn("GOOGLE_API_KEY", os.environ)
        self.assertNotIn("BRAVE_API_KEY", os.environ)


if __name__ == "__main__":
    unittest.main()
