"""Tests for versioned GUI task checkpoints."""

from __future__ import annotations

import unittest

from openppx.gui.checkpoint import (
    GUI_TASK_CHECKPOINT_SCHEMA,
    GUI_TASK_CHECKPOINT_SCHEMA_VERSION,
    build_gui_task_checkpoint,
    normalize_gui_task_checkpoint,
)


class GuiTaskCheckpointTests(unittest.TestCase):
    def test_normalize_accepts_legacy_checkpoint_payload(self) -> None:
        payload = normalize_gui_task_checkpoint(
            {
                "task": "submit form",
                "saved_info": {"username": "alice", "count": 3},
                "history": [{"step": 1}, "bad"],
            },
            max_steps=5,
            dry_run=True,
        )

        self.assertEqual(payload["schema"], GUI_TASK_CHECKPOINT_SCHEMA)
        self.assertEqual(payload["schema_version"], GUI_TASK_CHECKPOINT_SCHEMA_VERSION)
        self.assertEqual(payload["task"], "submit form")
        self.assertEqual(payload["max_steps"], 5)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["current_plan"], "submit form")
        self.assertEqual(payload["saved_info"], {"username": "alice", "count": "3"})
        self.assertEqual(payload["history"], [{"step": 1}])
        self.assertEqual(payload["next_step"], 2)
        self.assertEqual(payload["status_code"], "running")

    def test_build_checkpoint_keeps_runner_state_at_top_level(self) -> None:
        payload = build_gui_task_checkpoint(
            task="finish login",
            max_steps=8,
            dry_run=False,
            current_plan="click submit",
            saved_info={"username": "alice"},
            history=[{"step": 1, "type": "save_info"}],
            next_step=2,
            status_code="running",
        )

        self.assertEqual(payload["schema"], GUI_TASK_CHECKPOINT_SCHEMA)
        self.assertEqual(payload["schema_version"], GUI_TASK_CHECKPOINT_SCHEMA_VERSION)
        self.assertEqual(payload["task"], "finish login")
        self.assertEqual(payload["current_plan"], "click submit")
        self.assertEqual(payload["next_step"], 2)
        self.assertNotIn("payload", payload)

    def test_normalize_can_read_without_injecting_schema_for_runtime_state(self) -> None:
        payload = normalize_gui_task_checkpoint(
            {"task": "legacy", "history": []},
            include_schema=False,
        )

        self.assertNotIn("schema", payload)
        self.assertNotIn("schema_version", payload)
        self.assertEqual(payload["task"], "legacy")

    def test_normalize_rejects_unsupported_schema_version(self) -> None:
        with self.assertRaises(ValueError):
            normalize_gui_task_checkpoint(
                {
                    "schema": GUI_TASK_CHECKPOINT_SCHEMA,
                    "schema_version": 999,
                    "task": "future",
                }
            )


if __name__ == "__main__":
    unittest.main()
