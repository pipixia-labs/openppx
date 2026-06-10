"""Tests for durable long-task fact storage."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from openppx.runtime.task_store import (
    TaskArtifactStore,
    TaskCheckpointStore,
    TaskDeliveryStore,
    TaskEventStore,
    TaskInputStore,
    TaskStore,
    ToolCallRecordStore,
)


class TaskStoreTests(unittest.TestCase):
    def test_task_event_and_tool_call_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            tasks = TaskStore(db_path=db_path)
            events = TaskEventStore(db_path=db_path)
            calls = ToolCallRecordStore(db_path=db_path)

            task = tasks.create_task(
                kind="skill_api",
                status="running",
                title="demo:run",
                user_id="user-1",
                session_id="session-1",
                invocation_id="inv-1",
                function_call_id="fc-1",
                runner_payload={"runner": "process", "session_id": "proc-1"},
                runner_capabilities={"status": True, "checkpoint": False},
                resume_policy="rejoin",
                stop_policy="interrupt_task",
                cancel_policy="kill_process",
            )
            event = events.append_event(task.task_id, "task.started", message="started")

            self.assertEqual(tasks.get_task(task.task_id), task)
            self.assertEqual(task.runner_capabilities, {"status": True, "checkpoint": False})
            self.assertEqual(task.resume_policy, "rejoin")
            self.assertEqual(task.stop_policy, "interrupt_task")
            self.assertEqual(task.cancel_policy, "kill_process")
            self.assertEqual(event.event_type, "task.started")
            self.assertEqual(events.list_events(task.task_id)[0].message, "started")

            updated = tasks.update_task(
                task.task_id,
                expected_version=task.version,
                status="completed",
                terminal_summary="done",
            )
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, "completed")
            self.assertIsNotNone(updated.ended_at_ms)

            stale = tasks.update_task(
                task.task_id,
                expected_version=task.version,
                status="failed",
            )
            self.assertIsNone(stale)

            record, created = calls.create_or_get(
                idempotency_key="idem-1",
                tool_name="invoke_skill_api",
                args_hash="abc",
            )
            self.assertTrue(created)
            self.assertEqual(record.status, "pending")

            same, created_again = calls.create_or_get(
                idempotency_key="idem-1",
                tool_name="invoke_skill_api",
                args_hash="abc",
            )
            self.assertFalse(created_again)
            self.assertEqual(same.idempotency_key, "idem-1")

            linked = calls.link_task("idem-1", task.task_id)
            self.assertIsNotNone(linked)
            assert linked is not None
            self.assertEqual(linked.task_id, task.task_id)

    def test_task_store_migrates_capability_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE task_runs (
                        task_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        title TEXT NOT NULL,
                        owner_key TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        invocation_id TEXT NOT NULL,
                        function_call_id TEXT NOT NULL,
                        tool_call_id TEXT NOT NULL,
                        dedupe_key TEXT NOT NULL,
                        external_ref TEXT NOT NULL,
                        runner_payload_json TEXT NOT NULL,
                        lease_owner TEXT NOT NULL,
                        lease_expires_at_ms INTEGER,
                        claim_token TEXT NOT NULL,
                        progress_summary TEXT NOT NULL,
                        terminal_summary TEXT NOT NULL,
                        last_error TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        updated_at_ms INTEGER NOT NULL,
                        ended_at_ms INTEGER
                    )
                    """
                )

            TaskStore(db_path=db_path)

            with sqlite3.connect(db_path) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()}
            self.assertIn("runner_capabilities_json", columns)
            self.assertIn("resume_policy", columns)
            self.assertIn("stop_policy", columns)
            self.assertIn("cancel_policy", columns)
            self.assertIn("checkpoint_ref", columns)

    def test_task_inputs_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            tasks = TaskStore(db_path=db_path)
            inputs = TaskInputStore(db_path=db_path)
            task = tasks.create_task(kind="skill_api", status="waiting_user", title="demo")

            recorded = inputs.append_input(
                task.task_id,
                "use this file",
                payload={"source": "chat"},
            )
            consumed = inputs.mark_consumed(recorded.input_id, consumed_at_ms=1234)

            listed = inputs.list_inputs(task.task_id)
            unconsumed = inputs.list_inputs(task.task_id, include_consumed=False)

            self.assertEqual(recorded.content, "use this file")
            self.assertEqual(recorded.payload, {"source": "chat"})
            self.assertIsNotNone(consumed)
            assert consumed is not None
            self.assertEqual(consumed.consumed_at_ms, 1234)
            self.assertEqual(len(listed), 1)
            self.assertEqual(unconsumed, [])

    def test_task_artifacts_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            tasks = TaskStore(db_path=db_path)
            artifacts = TaskArtifactStore(db_path=db_path)
            task = tasks.create_task(kind="skill_api", status="completed", title="demo")
            artifact_path = Path(tmp) / "output.txt"
            artifact_path.write_text("hello", encoding="utf-8")

            recorded = artifacts.record_artifact(
                task_id=task.task_id,
                artifact_type="process_output",
                label="Process output",
                media_type="text/plain",
                path=str(artifact_path),
                size_bytes=artifact_path.stat().st_size,
                metadata={"source": "test"},
            )
            listed = artifacts.list_artifacts(task.task_id)

            self.assertEqual(recorded.task_id, task.task_id)
            self.assertEqual(recorded.metadata, {"source": "test"})
            self.assertEqual(recorded.size_bytes, 5)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].path, str(artifact_path))

    def test_task_checkpoints_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            tasks = TaskStore(db_path=db_path)
            checkpoints = TaskCheckpointStore(db_path=db_path)
            task = tasks.create_task(kind="browser", status="paused", title="demo")

            recorded = checkpoints.record_checkpoint(
                task_id=task.task_id,
                checkpoint_type="runner_state",
                runner_name="browser",
                payload={"target_id": "tab-1", "step": 3},
                summary="Paused after step 3.",
            )
            fetched = checkpoints.get_checkpoint(recorded.checkpoint_id)
            listed = checkpoints.list_checkpoints(task.task_id)

            self.assertIsNotNone(fetched)
            assert fetched is not None
            self.assertEqual(fetched.payload, {"target_id": "tab-1", "step": 3})
            self.assertEqual(fetched.summary, "Paused after step 3.")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].checkpoint_id, recorded.checkpoint_id)

    def test_checkpoint_retention_candidates_keep_current_and_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            tasks = TaskStore(db_path=db_path)
            checkpoints = TaskCheckpointStore(db_path=db_path)
            task = tasks.create_task(kind="browser", status="paused", title="demo", session_id="s1")
            records = [
                checkpoints.record_checkpoint(task_id=task.task_id, checkpoint_id=f"ckpt-{index}")
                for index in range(1, 5)
            ]
            tasks.update_task(task.task_id, checkpoint_ref=records[0].checkpoint_id)
            with sqlite3.connect(db_path) as conn:
                for index, record in enumerate(records, start=1):
                    conn.execute(
                        "UPDATE task_checkpoints SET created_at_ms = ? WHERE checkpoint_id = ?",
                        (index * 1000, record.checkpoint_id),
                    )

            candidates = checkpoints.list_retention_candidates(
                older_than_ms=5000,
                keep_latest_per_task=2,
                session_id="s1",
                now_ms=10_000,
            )
            candidate_count = checkpoints.count_retention_candidates(
                older_than_ms=5000,
                keep_latest_per_task=2,
                session_id="s1",
                now_ms=10_000,
            )
            deleted = checkpoints.delete_retention_checkpoints([records[0].checkpoint_id, records[1].checkpoint_id])

            self.assertEqual([candidate.checkpoint_id for candidate in candidates], [records[1].checkpoint_id])
            self.assertEqual(candidate_count, 1)
            self.assertEqual(deleted, 1)
            self.assertIsNotNone(checkpoints.get_checkpoint(records[0].checkpoint_id))
            self.assertIsNone(checkpoints.get_checkpoint(records[1].checkpoint_id))
            self.assertIsNotNone(checkpoints.get_checkpoint(records[2].checkpoint_id))
            self.assertIsNotNone(checkpoints.get_checkpoint(records[3].checkpoint_id))

    def test_claim_and_delivery_records_are_once_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            tasks = TaskStore(db_path=db_path)
            deliveries = TaskDeliveryStore(db_path=db_path)
            task = tasks.create_task(kind="skill_api", status="running", title="demo")

            claimed = tasks.claim_task(task.task_id, lease_owner="worker-a", lease_ms=10_000, now_ms=1000)
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed.lease_owner, "worker-a")
            self.assertEqual(claimed.updated_at_ms, task.updated_at_ms)

            blocked = tasks.claim_task(task.task_id, lease_owner="worker-b", lease_ms=10_000, now_ms=1001)
            self.assertIsNone(blocked)

            self.assertTrue(
                tasks.release_claim(
                    task.task_id,
                    lease_owner="worker-a",
                    claim_token=claimed.claim_token,
                )
            )
            released = tasks.get_task(task.task_id)
            self.assertIsNotNone(released)
            assert released is not None
            self.assertEqual(released.updated_at_ms, task.updated_at_ms)

            reclaimed = tasks.claim_task(task.task_id, lease_owner="worker-b", lease_ms=10_000, now_ms=1002)
            self.assertIsNotNone(reclaimed)
            assert reclaimed is not None
            self.assertEqual(reclaimed.updated_at_ms, task.updated_at_ms)

            first, created = deliveries.record_once(
                task_id=task.task_id,
                delivery_type="task.completed",
                payload={"status": "completed"},
            )
            second, created_again = deliveries.record_once(
                task_id=task.task_id,
                delivery_type="task.completed",
                payload={"status": "completed"},
            )

            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(first.delivery_key, second.delivery_key)
            self.assertEqual(len(deliveries.list_deliveries(task.task_id)), 1)

            failed = deliveries.mark_failed(
                first.delivery_key,
                error="temporary send failure",
                retry_after_ms=100,
                failed_at_ms=1_000,
            )
            self.assertIsNotNone(failed)
            assert failed is not None
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.attempts, 1)
            self.assertEqual(failed.last_error, "temporary send failure")
            self.assertEqual(failed.next_attempt_at_ms, 1_100)
            self.assertEqual(deliveries.list_retryable_deliveries(now_ms=1_099), [])
            self.assertEqual(len(deliveries.list_retryable_deliveries(now_ms=1_100)), 1)

            delivered = deliveries.mark_delivered(
                first.delivery_key,
                delivered_at_ms=1_200,
                ack_payload={"provider_message_id": "msg-1", "channel": "local"},
            )
            self.assertIsNotNone(delivered)
            assert delivered is not None
            self.assertEqual(delivered.status, "delivered")
            self.assertEqual(delivered.attempts, 2)
            self.assertEqual(delivered.last_error, "")
            self.assertEqual(delivered.delivered_at_ms, 1_200)
            self.assertEqual(delivered.ack_status, "provider_receipt")
            self.assertEqual(delivered.provider_message_id, "msg-1")
            self.assertEqual(delivered.ack_payload["channel"], "local")
            self.assertEqual(delivered.acked_at_ms, 1_200)
            self.assertEqual(deliveries.list_retryable_deliveries(now_ms=1_200), [])

            summaries = deliveries.summarize_by_task_ids([task.task_id, "missing-task"])
            self.assertEqual(set(summaries), {task.task_id})
            summary = summaries[task.task_id]
            self.assertEqual(summary["count"], 1)
            self.assertEqual(summary["delivered_count"], 1)
            self.assertEqual(summary["failed_count"], 0)
            self.assertEqual(summary["latest"]["status"], "delivered")
            self.assertEqual(summary["latest"]["attempts"], 2)
            self.assertEqual(summary["latest"]["ack_status"], "provider_receipt")
            self.assertEqual(summary["latest"]["provider_message_id"], "msg-1")

    def test_status_stuck_and_terminal_cleanup_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            tasks = TaskStore(db_path=db_path)
            events = TaskEventStore(db_path=db_path)
            inputs = TaskInputStore(db_path=db_path)
            deliveries = TaskDeliveryStore(db_path=db_path)
            artifacts = TaskArtifactStore(db_path=db_path)
            checkpoints = TaskCheckpointStore(db_path=db_path)
            calls = ToolCallRecordStore(db_path=db_path)

            running = tasks.create_task(kind="skill_api", status="running", title="running", session_id="s1")
            completed = tasks.create_task(kind="skill_api", status="completed", title="done", session_id="s1")
            recent_failed = tasks.create_task(kind="skill_api", status="failed", title="recent", session_id="s1")
            other_session = tasks.create_task(kind="skill_api", status="completed", title="other", session_id="s2")
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE task_runs SET updated_at_ms = ? WHERE task_id = ?", (1000, running.task_id))
                conn.execute("UPDATE task_runs SET updated_at_ms = ? WHERE task_id = ?", (1000, completed.task_id))
                conn.execute("UPDATE task_runs SET updated_at_ms = ? WHERE task_id = ?", (4900, recent_failed.task_id))
                conn.execute("UPDATE task_runs SET updated_at_ms = ? WHERE task_id = ?", (1000, other_session.task_id))
            events.append_event(completed.task_id, "task.completed", message="done")
            inputs.append_input(completed.task_id, "ignored after terminal")
            deliveries.record_once(task_id=completed.task_id, delivery_type="task.completed", payload={})
            artifacts.record_artifact(
                task_id=completed.task_id,
                artifact_type="process_output",
                label="Process output",
                media_type="text/plain",
                path=str(Path(tmp) / "output.txt"),
                size_bytes=1,
            )
            checkpoints.record_checkpoint(task_id=completed.task_id, summary="terminal checkpoint")
            calls.create_or_get(idempotency_key="idem-cleanup", tool_name="invoke_skill_api", args_hash="abc")
            calls.link_task("idem-cleanup", completed.task_id)

            counts = tasks.count_by_status(session_id="s1")
            stuck = tasks.list_stuck_tasks(session_id="s1", older_than_ms=2000, now_ms=5000)
            terminal = tasks.list_terminal_tasks_older_than(session_id="s1", older_than_ms=2000, now_ms=5000)
            deleted = tasks.delete_tasks([task.task_id for task in terminal])

            self.assertEqual(counts["running"], 1)
            self.assertEqual(counts["completed"], 1)
            self.assertEqual(counts["failed"], 1)
            self.assertEqual([task.task_id for task in stuck], [running.task_id])
            self.assertEqual([task.task_id for task in terminal], [completed.task_id])
            self.assertEqual(deleted, 1)
            self.assertIsNone(tasks.get_task(completed.task_id))
            self.assertEqual(events.list_events(completed.task_id), [])
            self.assertEqual(inputs.list_inputs(completed.task_id), [])
            self.assertEqual(deliveries.list_deliveries(completed.task_id), [])
            self.assertEqual(artifacts.list_artifacts(completed.task_id), [])
            self.assertEqual(checkpoints.list_checkpoints(completed.task_id), [])
            record = calls.get_record("idem-cleanup")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.task_id, completed.task_id)
            self.assertIsNotNone(tasks.get_task(recent_failed.task_id))
            self.assertIsNotNone(tasks.get_task(other_session.task_id))

    def test_orphaned_artifact_and_checkpoint_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            TaskStore(db_path=db_path)
            artifacts = TaskArtifactStore(db_path=db_path)
            checkpoints = TaskCheckpointStore(db_path=db_path)
            artifact_path = Path(tmp) / "orphan.txt"
            artifact_path.write_text("orphan", encoding="utf-8")

            artifact = artifacts.record_artifact(
                task_id="missing-task",
                artifact_type="process_output",
                label="Orphan output",
                media_type="text/plain",
                path=str(artifact_path),
                size_bytes=artifact_path.stat().st_size,
            )
            checkpoint = checkpoints.record_checkpoint(
                task_id="missing-task",
                checkpoint_type="runner_state",
                runner_name="fake",
                payload={"step": 2},
            )

            self.assertEqual(artifacts.count_orphaned_artifacts(), 1)
            self.assertEqual(checkpoints.count_orphaned_checkpoints(), 1)
            self.assertEqual(artifacts.list_orphaned_artifacts()[0].artifact_id, artifact.artifact_id)
            self.assertEqual(checkpoints.list_orphaned_checkpoints()[0].checkpoint_id, checkpoint.checkpoint_id)
            self.assertEqual(artifacts.delete_artifact_records([artifact.artifact_id]), 1)
            self.assertEqual(checkpoints.delete_checkpoints([checkpoint.checkpoint_id]), 1)
            self.assertEqual(artifacts.count_orphaned_artifacts(), 0)
            self.assertEqual(checkpoints.count_orphaned_checkpoints(), 0)


if __name__ == "__main__":
    unittest.main()
