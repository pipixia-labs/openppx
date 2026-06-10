"""Tests for durable GUI job coordination."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from openppx.gui.checkpoint import GUI_TASK_CHECKPOINT_SCHEMA, GUI_TASK_CHECKPOINT_SCHEMA_VERSION
from openppx.gui.job_coordinator import (
    GuiJobStore,
    gui_task_job_cancel,
    gui_task_job_output,
    gui_task_job_status,
    resume_gui_task_job,
    submit_gui_task_job,
)


class GuiJobCoordinatorTests(unittest.TestCase):
    def test_submit_status_and_output_for_completed_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiJobStore(db_path=Path(tmp) / "gui_jobs.db")

            def _executor(**kwargs: Any) -> dict[str, Any]:
                kwargs["checkpoint_callback"](
                    {
                        "task": kwargs["task"],
                        "current_plan": "finish quickly",
                        "history": [],
                        "next_step": 1,
                        "summary": "checkpointed",
                    }
                )
                return {
                    "ok": True,
                    "finished": True,
                    "status_code": "completed",
                    "message": "done",
                    "final_summary": "done",
                }

            submitted = submit_gui_task_job(
                task="finish login",
                max_steps=2,
                dry_run=True,
                store=store,
                executor=_executor,
            )
            status = self._wait_for_status(store, submitted["job_id"], "completed")
            output = gui_task_job_output(submitted["job_id"], store=store)

            self.assertTrue(submitted["ok"])
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["summary"], "done")
            self.assertEqual(status["checkpoint"]["schema"], GUI_TASK_CHECKPOINT_SCHEMA)
            self.assertEqual(status["checkpoint"]["schema_version"], GUI_TASK_CHECKPOINT_SCHEMA_VERSION)
            self.assertEqual(status["checkpoint"]["current_plan"], "finish quickly")
            self.assertEqual(output["status"], "completed")
            self.assertEqual(output["output"]["message"], "done")

    def test_cancel_running_job_can_pause_at_checkpoint_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiJobStore(db_path=Path(tmp) / "gui_jobs.db")
            started = threading.Event()

            def _executor(**kwargs: Any) -> dict[str, Any]:
                kwargs["checkpoint_callback"](
                    {
                        "task": kwargs["task"],
                        "current_plan": "pauseable plan",
                        "history": [{"step": 1, "type": "execute"}],
                        "next_step": 2,
                        "summary": "paused after step 1",
                    }
                )
                started.set()
                token = kwargs["cancel_token"]
                while True:
                    token.check_cancelled()
                    time.sleep(0.01)

            submitted = submit_gui_task_job(
                task="pause browser flow",
                max_steps=4,
                store=store,
                executor=_executor,
            )
            self.assertTrue(started.wait(timeout=1.0))
            cancelled = gui_task_job_cancel(
                submitted["job_id"],
                terminal_status="paused",
                reason="test pause",
                store=store,
            )
            status = self._wait_for_status(store, submitted["job_id"], "paused")

            self.assertTrue(cancelled["ok"])
            self.assertEqual(cancelled["action"], "paused")
            self.assertEqual(status["status"], "paused")
            self.assertEqual(status["checkpoint"]["schema"], GUI_TASK_CHECKPOINT_SCHEMA)
            self.assertEqual(status["checkpoint"]["schema_version"], GUI_TASK_CHECKPOINT_SCHEMA_VERSION)
            self.assertEqual(status["checkpoint"]["next_step"], 2)

    def test_resume_submits_new_job_with_checkpoint_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiJobStore(db_path=Path(tmp) / "gui_jobs.db")
            observed: dict[str, Any] = {}
            checkpoint = {
                "task": "resume browser flow",
                "max_steps": 5,
                "dry_run": True,
                "current_plan": "continue from step 2",
                "saved_info": {"username": "alice"},
                "history": [{"step": 1, "type": "save_info"}],
                "next_step": 2,
                "job_id": "gui_job_previous",
            }

            def _executor(**kwargs: Any) -> dict[str, Any]:
                observed["initial_state"] = kwargs["initial_state"]
                return {
                    "ok": True,
                    "finished": True,
                    "status_code": "completed",
                    "message": "resumed",
                    "final_summary": "resumed",
                }

            resumed = resume_gui_task_job(
                checkpoint=checkpoint,
                store=store,
                executor=_executor,
            )
            status = self._wait_for_status(store, resumed["job_id"], "completed")

            self.assertTrue(resumed["ok"])
            self.assertNotEqual(resumed["job_id"], "gui_job_previous")
            self.assertEqual(status["request"]["parent_job_id"], "gui_job_previous")
            self.assertEqual(observed["initial_state"]["schema"], GUI_TASK_CHECKPOINT_SCHEMA)
            self.assertEqual(observed["initial_state"]["schema_version"], GUI_TASK_CHECKPOINT_SCHEMA_VERSION)
            self.assertEqual(observed["initial_state"]["saved_info"], {"username": "alice"})
            self.assertEqual(observed["initial_state"]["history"][0]["step"], 1)

    def test_resume_rejects_unsupported_checkpoint_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiJobStore(db_path=Path(tmp) / "gui_jobs.db")

            result = resume_gui_task_job(
                checkpoint={
                    "schema": GUI_TASK_CHECKPOINT_SCHEMA,
                    "schema_version": 999,
                    "task": "future checkpoint",
                },
                store=store,
            )

            self.assertFalse(result["ok"])
            self.assertIn("unsupported GUI task checkpoint", result["error"])

    def _wait_for_status(self, store: GuiJobStore, job_id: str, expected: str) -> dict[str, Any]:
        for _ in range(80):
            status = gui_task_job_status(job_id, store=store)
            if status.get("status") == expected:
                return status
            time.sleep(0.02)
        return gui_task_job_status(job_id, store=store)


if __name__ == "__main__":
    unittest.main()
