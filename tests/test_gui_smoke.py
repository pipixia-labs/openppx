from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from openpipixia.core.config import default_config, save_config
from scripts import gui_smoke


class GuiSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_bootstrap_uses_explicit_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "agent_a" / "config.json"
            cfg = default_config()
            cfg["multimodalProviders"]["openai_mm"]["enabled"] = True
            cfg["multimodalProviders"]["openai_mm"]["apiKey"] = "test-key"
            cfg["multimodalProviders"]["openai_mm"]["model"] = "gpt-5.4"
            save_config(cfg, config_path)

            resolved = gui_smoke.bootstrap_gui_smoke_env(str(config_path))

        self.assertEqual(resolved, config_path)
        self.assertEqual(os.environ["OPENPIPIXIA_GUI_MODEL"], "gpt-5.4")
        self.assertEqual(os.environ["OPENPIPIXIA_GUI_PLANNER_MODEL"], "gpt-5.4")
        self.assertEqual(os.environ["OPENAI_API_KEY"], "test-key")

    def test_bootstrap_falls_back_to_first_enabled_agent_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / ".openpipixia"
            data_dir.mkdir(parents=True, exist_ok=True)
            os.environ["OPENPIPIXIA_DATA_DIR"] = str(data_dir)
            global_config = {
                "agents": [
                    {"name": "agent_name_1", "enabled": True},
                    {"name": "agent_name_2", "enabled": False},
                ]
            }
            (data_dir / "global_config.json").write_text(json.dumps(global_config), encoding="utf-8")
            config_path = data_dir / "agent_name_1" / "config.json"
            cfg = default_config()
            cfg["multimodalProviders"]["openai_mm"]["enabled"] = True
            cfg["multimodalProviders"]["openai_mm"]["apiKey"] = "agent-key"
            cfg["multimodalProviders"]["openai_mm"]["model"] = "gpt-5.4"
            save_config(cfg, config_path)

            resolved = gui_smoke.bootstrap_gui_smoke_env()

        self.assertEqual(resolved, config_path)
        self.assertEqual(os.environ["OPENPIPIXIA_CONFIG_FILE"], str(config_path.resolve()))
        self.assertEqual(os.environ["OPENPIPIXIA_GUI_MODEL"], "gpt-5.4")

    def test_enable_debug_log_sets_env(self) -> None:
        path = gui_smoke._enable_debug_log("/tmp/gui-smoke-debug.log")
        self.assertEqual(path, "/tmp/gui-smoke-debug.log")
        self.assertEqual(os.environ["OPENPIPIXIA_DEBUG"], "1")
        self.assertEqual(os.environ["OPENPIPIXIA_DEBUG_LOG_PATH"], "/tmp/gui-smoke-debug.log")


if __name__ == "__main__":
    unittest.main()
