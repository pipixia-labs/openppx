"""Tests for GUI prompt loading."""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from openpipixia.gui.prompts import (
    DEFAULT_GUI_EXECUTOR_SYSTEM_PROMPT_PATH_ENV,
    DEFAULT_GUI_PLANNER_SYSTEM_PROMPT_PATH_ENV,
    load_executor_system_prompt,
    load_planner_system_prompt,
)


class GuiPromptTests(unittest.TestCase):
    def test_loads_default_prompt_files(self) -> None:
        executor_prompt = load_executor_system_prompt()
        planner_prompt = load_planner_system_prompt()

        self.assertIn("desktop action grounding model", executor_prompt)
        self.assertIn('"action":"..."', executor_prompt)
        self.assertNotIn('"name":"computer_use"', executor_prompt)
        self.assertIn("GUI task planner", planner_prompt)

    def test_env_override_prompt_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor_path = Path(tmp) / "executor.md"
            planner_path = Path(tmp) / "planner.md"
            executor_path.write_text("custom executor prompt", encoding="utf-8")
            planner_path.write_text("custom planner prompt", encoding="utf-8")

            with unittest.mock.patch.dict(
                "os.environ",
                {
                    DEFAULT_GUI_EXECUTOR_SYSTEM_PROMPT_PATH_ENV: str(executor_path),
                    DEFAULT_GUI_PLANNER_SYSTEM_PROMPT_PATH_ENV: str(planner_path),
                },
                clear=False,
            ):
                self.assertEqual(load_executor_system_prompt(), "custom executor prompt")
                self.assertEqual(load_planner_system_prompt(), "custom planner prompt")


if __name__ == "__main__":
    unittest.main()
