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
        self.assertEqual(cfg["channels"]["enabled"], ["local"])
        self.assertEqual(cfg["session"]["backend"], "memory")

    def test_save_then_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["channels"]["enabled"] = ["feishu"]
            cfg["channels"]["feishu"]["appId"] = "app-id-1"
            cfg["channels"]["feishu"]["appSecret"] = "app-secret-1"
            save_config(cfg, path)
            loaded = load_config(path)

        self.assertEqual(loaded["channels"]["enabled"], ["feishu"])
        self.assertEqual(loaded["channels"]["feishu"]["appId"], "app-id-1")
        self.assertEqual(loaded["channels"]["feishu"]["appSecret"], "app-secret-1")

    def test_apply_config_to_env_respects_existing_values(self) -> None:
        os.environ["SENTIENTAGENT_V2_MODEL"] = "from-shell"
        cfg = default_config()
        cfg["agent"]["model"] = "from-config"
        apply_config_to_env(cfg, overwrite=False)
        self.assertEqual(os.environ["SENTIENTAGENT_V2_MODEL"], "from-shell")

        apply_config_to_env(cfg, overwrite=True)
        self.assertEqual(os.environ["SENTIENTAGENT_V2_MODEL"], "from-config")

    def test_bootstrap_env_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["channels"]["enabled"] = ["feishu"]
            cfg["channels"]["feishu"]["appId"] = "app-id"
            cfg["channels"]["feishu"]["appSecret"] = "app-secret"
            cfg["session"]["backend"] = "sqlite"
            save_config(cfg, path)

            os.environ.pop("SENTIENTAGENT_V2_CHANNELS", None)
            os.environ.pop("FEISHU_APP_ID", None)
            os.environ.pop("SENTIENTAGENT_V2_SESSION_BACKEND", None)
            loaded = bootstrap_env_from_config(path)

        self.assertIsNotNone(loaded)
        self.assertEqual(os.environ["SENTIENTAGENT_V2_CHANNELS"], "feishu")
        self.assertEqual(os.environ["FEISHU_APP_ID"], "app-id")
        self.assertEqual(os.environ["SENTIENTAGENT_V2_SESSION_BACKEND"], "sqlite")


if __name__ == "__main__":
    unittest.main()
