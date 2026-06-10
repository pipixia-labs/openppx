"""Tests for long-task scheduler and delivery maintenance."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from openppx.runtime.checkpoint_schema import TASK_CHECKPOINT_ENVELOPE_SCHEMA, TASK_CHECKPOINT_METADATA_KEY
from openppx.runtime.task_scheduler import TaskWakeScheduler
from openppx.runtime.task_store import TaskCheckpointStore, TaskDeliveryStore, TaskStore
from openppx.tooling.registry import invoke_skill_api, list_tasks, show_task


class TaskWakeSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_drain_due_tasks_syncs_completed_process_and_records_delivery_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_skill(
                tmp,
                "finish",
                "import time\nprint('started', flush=True)\ntime.sleep(0.05)\nprint('done', flush=True)\n",
            )
            payload = json.loads(invoke_skill_api("demo", "finish", inline_budget_ms=0))
            self.assertEqual(payload["mode"], "task")
            task_id = payload["task_id"]
            time.sleep(0.2)

            store = TaskStore()
            deliveries = TaskDeliveryStore(db_path=store.db_path)
            delivered_payloads: list[dict[str, object]] = []
            def on_delivery(payload_to_send: dict[str, object]) -> dict[str, object]:
                delivered_payloads.append(payload_to_send)
                return {"provider_message_id": "local-msg-1", "provider": "local"}

            scheduler = TaskWakeScheduler(
                task_store=store,
                delivery_store=deliveries,
                lease_ms=2_000,
                owner="test-worker",
                on_delivery=on_delivery,
            )

            first = asyncio.run(scheduler.drain_due_tasks())
            second = asyncio.run(scheduler.drain_due_tasks())
            shown = json.loads(show_task(task_id))
            listed = json.loads(list_tasks())
            listed_item = next(item for item in listed["items"] if item["task_id"] == task_id)

            self.assertEqual(first.claimed, 1)
            self.assertEqual(first.deliveries, 1)
            self.assertEqual(second.deliveries, 0)
            self.assertEqual(shown["task"]["status"], "completed")
            self.assertEqual(shown["task"]["delivery_summary"]["latest"]["status"], "delivered")
            self.assertEqual(listed_item["delivery_summary"]["latest"]["status"], "delivered")
            self.assertEqual(listed_item["delivery_summary"]["delivered_count"], 1)
            self.assertEqual(len(shown["deliveries"]), 1)
            self.assertEqual(shown["deliveries"][0]["status"], "delivered")
            self.assertEqual(shown["deliveries"][0]["delivery_type"], "task.completed")
            self.assertEqual(shown["deliveries"][0]["payload"]["task_id"], task_id)
            self.assertEqual(shown["deliveries"][0]["ack_status"], "provider_receipt")
            self.assertEqual(shown["deliveries"][0]["provider_message_id"], "local-msg-1")
            self.assertEqual(shown["task"]["delivery_summary"]["latest"]["ack_status"], "provider_receipt")
            self.assertEqual(len(deliveries.list_deliveries(task_id)), 1)
            self.assertEqual(len(delivered_payloads), 1)
            self.assertEqual(delivered_payloads[0]["task_id"], task_id)
            self.assertEqual(delivered_payloads[0]["status"], "completed")

    def test_drain_due_tasks_marks_eligible_stale_process_task_lost_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            deliveries = TaskDeliveryStore(db_path=store.db_path)
            task = store.create_task(
                kind="skill_api",
                status="stale",
                title="demo:missing",
                external_ref="missing-session",
                runner_payload={"runner": "process", "delivery": {"channel": "local", "chat_id": "tasks"}},
                progress_summary="Backing process session not found.",
            )
            delivered_payloads: list[dict[str, object]] = []
            scheduler = TaskWakeScheduler(
                task_store=store,
                delivery_store=deliveries,
                stale_lost_after_ms=0,
                lease_ms=2_000,
                owner="test-worker",
                on_delivery=lambda payload: delivered_payloads.append(payload),
            )

            first = asyncio.run(scheduler.drain_due_tasks())
            second = asyncio.run(scheduler.drain_due_tasks())
            updated = store.get_task(task.task_id)

            self.assertEqual(first.claimed, 1)
            self.assertEqual(first.deliveries, 1)
            self.assertEqual(second.deliveries, 0)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, "lost")
            self.assertEqual(len(deliveries.list_deliveries(task.task_id)), 1)
            self.assertEqual(len(delivered_payloads), 1)
            self.assertEqual(delivered_payloads[0]["status"], "lost")
            self.assertEqual(delivered_payloads[0]["delivery"], {"channel": "local", "chat_id": "tasks"})

    def test_failed_task_delivery_is_retried_on_later_drain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_skill(
                tmp,
                "finish",
                "import time\nprint('started', flush=True)\ntime.sleep(0.05)\nprint('done', flush=True)\n",
            )
            payload = json.loads(invoke_skill_api("demo", "finish", inline_budget_ms=0))
            task_id = payload["task_id"]
            time.sleep(0.2)

            store = TaskStore()
            deliveries = TaskDeliveryStore(db_path=store.db_path)
            attempts = 0
            delivered_payloads: list[dict[str, object]] = []

            def on_delivery(delivery_payload: dict[str, object]) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("send failed")
                delivered_payloads.append(delivery_payload)

            scheduler = TaskWakeScheduler(
                task_store=store,
                delivery_store=deliveries,
                lease_ms=2_000,
                owner="test-worker",
                delivery_retry_base_ms=1,
                delivery_retry_max_ms=1,
                on_delivery=on_delivery,
            )

            first = asyncio.run(scheduler.drain_due_tasks())
            first_delivery = deliveries.list_deliveries(task_id)[0]
            failed_shown = json.loads(show_task(task_id))
            failed_listed = json.loads(list_tasks())
            failed_item = next(item for item in failed_listed["items"] if item["task_id"] == task_id)
            time.sleep(0.01)
            second = asyncio.run(scheduler.drain_due_tasks())
            second_delivery = deliveries.list_deliveries(task_id)[0]
            delivered_shown = json.loads(show_task(task_id))
            delivered_listed = json.loads(list_tasks())
            delivered_item = next(item for item in delivered_listed["items"] if item["task_id"] == task_id)

            self.assertEqual(first.deliveries, 0)
            self.assertEqual(first_delivery.status, "failed")
            self.assertEqual(first_delivery.attempts, 1)
            self.assertIn("send failed", first_delivery.last_error)
            self.assertEqual(failed_shown["deliveries"][0]["status"], "failed")
            self.assertIn("send failed", failed_shown["deliveries"][0]["last_error"])
            self.assertEqual(failed_item["delivery_summary"]["latest"]["status"], "failed")
            self.assertEqual(failed_item["delivery_summary"]["failed_count"], 1)
            self.assertEqual(second.deliveries, 1)
            self.assertEqual(second_delivery.status, "delivered")
            self.assertEqual(second_delivery.attempts, 2)
            self.assertEqual(delivered_shown["deliveries"][0]["status"], "delivered")
            self.assertEqual(delivered_shown["deliveries"][0]["last_error"], "")
            self.assertEqual(delivered_item["delivery_summary"]["latest"]["status"], "delivered")
            self.assertEqual(delivered_item["delivery_summary"]["delivered_count"], 1)
            self.assertEqual(len(delivered_payloads), 1)
            self.assertEqual(delivered_payloads[0]["task_id"], task_id)

    def test_drain_due_tasks_syncs_completed_gui_job_and_records_delivery_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            deliveries = TaskDeliveryStore(db_path=store.db_path)
            task = store.create_task(
                kind="gui_task",
                status="running",
                title="GUI workflow",
                external_ref="gui_job_done",
                runner_payload={
                    "runner": "gui_job",
                    "job_id": "gui_job_done",
                    "delivery": {"channel": "local", "chat_id": "tasks"},
                },
                runner_capabilities={"status": True, "output": True, "pause": True, "checkpoint": True},
                resume_policy="checkpoint",
                progress_summary="GUI job running.",
            )
            delivered_payloads: list[dict[str, object]] = []
            scheduler = TaskWakeScheduler(
                task_store=store,
                delivery_store=deliveries,
                lease_ms=2_000,
                owner="test-worker",
                on_delivery=lambda payload: delivered_payloads.append(payload),
            )

            with mock.patch(
                "openppx.runtime.task_execution.gui_task_job_status",
                return_value={
                    "ok": True,
                    "job_id": "gui_job_done",
                    "status": "completed",
                    "summary": "GUI job completed.",
                    "result": {"final_summary": "GUI job completed."},
                    "checkpoint": {},
                },
            ):
                first = asyncio.run(scheduler.drain_due_tasks())
                second = asyncio.run(scheduler.drain_due_tasks())

            updated = store.get_task(task.task_id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, "completed")
            self.assertEqual(first.deliveries, 1)
            self.assertEqual(second.deliveries, 0)
            self.assertEqual(deliveries.list_deliveries(task.task_id)[0].delivery_type, "task.completed")
            self.assertEqual(delivered_payloads[0]["status"], "completed")

    def test_drain_due_tasks_syncs_paused_gui_job_records_checkpoint_and_delivery_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            deliveries = TaskDeliveryStore(db_path=store.db_path)
            task = store.create_task(
                kind="gui_task",
                status="running",
                title="GUI workflow",
                external_ref="gui_job_paused",
                runner_payload={
                    "runner": "gui_job",
                    "job_id": "gui_job_paused",
                    "delivery": {"channel": "local", "chat_id": "tasks"},
                },
                runner_capabilities={"status": True, "output": True, "pause": True, "checkpoint": True, "resume": True},
                resume_policy="checkpoint",
                progress_summary="GUI job running.",
            )
            checkpoint = {
                "task": "GUI workflow",
                "current_plan": "continue from step 2",
                "history": [{"step": 1, "type": "execute"}],
                "next_step": 2,
            }
            delivered_payloads: list[dict[str, object]] = []
            scheduler = TaskWakeScheduler(
                task_store=store,
                delivery_store=deliveries,
                lease_ms=2_000,
                owner="test-worker",
                on_delivery=lambda payload: delivered_payloads.append(payload),
            )

            with mock.patch(
                "openppx.runtime.task_execution.gui_task_job_status",
                return_value={
                    "ok": True,
                    "job_id": "gui_job_paused",
                    "status": "paused",
                    "summary": "Paused at step 1.",
                    "result": {},
                    "checkpoint": checkpoint,
                },
            ):
                first = asyncio.run(scheduler.drain_due_tasks())
                second = asyncio.run(scheduler.drain_due_tasks())

            updated = store.get_task(task.task_id)
            checkpoints = TaskCheckpointStore(db_path=store.db_path).list_checkpoints(task.task_id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, "paused")
            self.assertTrue(updated.checkpoint_ref)
            self.assertEqual(first.deliveries, 1)
            self.assertEqual(second.deliveries, 0)
            self.assertEqual(deliveries.list_deliveries(task.task_id)[0].delivery_type, "task.paused")
            self.assertEqual(delivered_payloads[0]["status"], "paused")
            self.assertEqual(delivered_payloads[0]["delivery"], {"channel": "local", "chat_id": "tasks"})
            self.assertEqual(checkpoints[0].payload["task"], checkpoint["task"])
            self.assertEqual(checkpoints[0].payload["history"], checkpoint["history"])
            self.assertEqual(checkpoints[0].payload["next_step"], checkpoint["next_step"])
            self.assertEqual(
                checkpoints[0].payload[TASK_CHECKPOINT_METADATA_KEY]["schema"],
                TASK_CHECKPOINT_ENVELOPE_SCHEMA,
            )

    def test_drain_due_tasks_delivers_gui_job_stale_then_lost_once_each(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            deliveries = TaskDeliveryStore(db_path=store.db_path)
            task = store.create_task(
                kind="gui_task",
                status="running",
                title="GUI workflow",
                external_ref="gui_job_missing",
                runner_payload={
                    "runner": "gui_job",
                    "job_id": "gui_job_missing",
                    "delivery": {"channel": "local", "chat_id": "tasks"},
                },
                runner_capabilities={"status": True, "output": True, "pause": True, "checkpoint": True},
                resume_policy="checkpoint",
                progress_summary="GUI job running.",
            )
            delivered_payloads: list[dict[str, object]] = []
            scheduler = TaskWakeScheduler(
                task_store=store,
                delivery_store=deliveries,
                stale_lost_after_ms=0,
                lease_ms=2_000,
                owner="test-worker",
                on_delivery=lambda payload: delivered_payloads.append(payload),
            )

            with mock.patch(
                "openppx.runtime.task_execution.gui_task_job_status",
                return_value={"ok": False, "error": "GUI job is not attached to this process."},
            ):
                first = asyncio.run(scheduler.drain_due_tasks())
                second = asyncio.run(scheduler.drain_due_tasks())
                third = asyncio.run(scheduler.drain_due_tasks())

            updated = store.get_task(task.task_id)
            delivery_types = [delivery.delivery_type for delivery in deliveries.list_deliveries(task.task_id)]
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, "lost")
            self.assertEqual(first.deliveries, 1)
            self.assertEqual(second.deliveries, 1)
            self.assertEqual(third.deliveries, 0)
            self.assertEqual(delivery_types, ["task.stale", "task.lost"])
            self.assertEqual([payload["status"] for payload in delivered_payloads], ["stale", "lost"])

    def test_checkpoint_retention_cleanup_runs_only_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path=db_path)
            checkpoints = TaskCheckpointStore(db_path=store.db_path)
            task = store.create_task(kind="browser", status="paused", title="demo")
            records = [
                checkpoints.record_checkpoint(task_id=task.task_id, checkpoint_id=f"ckpt-{index}")
                for index in range(1, 5)
            ]
            store.update_task(task.task_id, checkpoint_ref=records[0].checkpoint_id)
            with sqlite3.connect(db_path) as conn:
                for index, record in enumerate(records, start=1):
                    conn.execute(
                        "UPDATE task_checkpoints SET created_at_ms = ? WHERE checkpoint_id = ?",
                        (index * 1000, record.checkpoint_id),
                    )

            disabled = TaskWakeScheduler(task_store=store, checkpoint_retention_enabled=False)
            disabled_result = asyncio.run(disabled.drain_due_tasks())

            self.assertEqual(disabled_result.checkpoint_retention_deleted, 0)
            self.assertEqual(len(checkpoints.list_checkpoints(task.task_id)), 4)

            enabled = TaskWakeScheduler(
                task_store=store,
                checkpoint_retention_enabled=True,
                checkpoint_retention_interval_seconds=1,
                checkpoint_retention_older_than_ms=0,
                checkpoint_retention_keep_latest_per_task=1,
                checkpoint_retention_batch_size=10,
            )
            first = asyncio.run(enabled.drain_due_tasks())
            second = asyncio.run(enabled.drain_due_tasks())
            remaining_ids = {checkpoint.checkpoint_id for checkpoint in checkpoints.list_checkpoints(task.task_id)}

            self.assertEqual(first.checkpoint_retention_deleted, 2)
            self.assertEqual(second.checkpoint_retention_deleted, 0)
            self.assertEqual(enabled.status()["last_result"]["checkpoint_retention_deleted"], 0)
            self.assertEqual(remaining_ids, {records[0].checkpoint_id, records[3].checkpoint_id})

    def _prepare_skill(self, tmp: str, api_name: str, script: str) -> None:
        root = Path(tmp)
        agent_home = root / "agent"
        scripts = agent_home / "skills" / "demo" / "scripts"
        scripts.mkdir(parents=True)
        (scripts.parent / "SKILL.md").write_text(
            "---\ndescription: demo skill\n---\n# Demo\n",
            encoding="utf-8",
        )
        (scripts / f"{api_name}.py").write_text(script, encoding="utf-8")
        os.environ["OPENPPX_AGENT_HOME"] = str(agent_home)
        os.environ["OPENPPX_TASK_DB_PATH"] = str(root / "tasks.db")


if __name__ == "__main__":
    unittest.main()
