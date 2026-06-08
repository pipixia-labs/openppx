"""Tests for short-term long-task context engine facts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openppx.runtime.context_engine import LongTaskContextStore


class ContextEngineTests(unittest.TestCase):
    def test_goal_and_todos_roundtrip_with_single_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LongTaskContextStore(db_path=Path(tmp) / "tasks.db")

            goal = store.upsert_goal(
                session_id="session-1",
                objective="Implement long task context",
                completion_criteria="Tests pass",
                current_summary="Planning",
            )
            todos = store.replace_todos(
                session_id="session-1",
                goal_id=goal.goal_id,
                items=[
                    {"content": "Design store", "status": "in_progress"},
                    {"content": "Write tests", "status": "in_progress"},
                    {"content": "Run pytest", "status": "pending"},
                ],
            )

            self.assertEqual(store.get_active_goal("session-1").goal_id, goal.goal_id)  # type: ignore[union-attr]
            self.assertEqual([item.status for item in todos], ["in_progress", "pending", "pending"])
            self.assertEqual([item.content for item in todos], ["Design store", "Write tests", "Run pytest"])

    def test_replace_todos_promotes_first_pending_when_no_active_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LongTaskContextStore(db_path=Path(tmp) / "tasks.db")

            todos = store.replace_todos(session_id="session-1", items=["First", "Second"])

            self.assertEqual([item.status for item in todos], ["in_progress", "pending"])

    def test_complete_goal_marks_todos_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LongTaskContextStore(db_path=Path(tmp) / "tasks.db")
            goal = store.upsert_goal(session_id="session-1", objective="Ship feature")
            store.replace_todos(session_id="session-1", goal_id=goal.goal_id, items=["Build", "Test"])

            completed = store.complete_goal(session_id="session-1", final_summary="Feature shipped")
            todos = store.list_todos(session_id="session-1", goal_id=goal.goal_id)

            self.assertIsNotNone(completed)
            assert completed is not None
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.current_summary, "Feature shipped")
            self.assertEqual([item.status for item in todos], ["completed", "completed"])

    def test_task_flow_roundtrip_promotes_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LongTaskContextStore(db_path=Path(tmp) / "tasks.db")

            flow, steps = store.upsert_flow(
                session_id="session-1",
                goal="Ship long task runtime",
                steps=[
                    {"title": "Design", "status": "in_progress"},
                    {"title": "Build", "status": "pending"},
                ],
            )
            updated_flow, updated_step = store.update_flow_step(
                flow_id=flow.flow_id,
                step_id=steps[0].step_id,
                status="completed",
                evidence={"tests": "targeted"},
            )
            current_steps = store.list_flow_steps(flow_id=flow.flow_id)

            self.assertEqual(flow.status, "running")
            self.assertEqual([step.status for step in steps], ["in_progress", "pending"])
            self.assertEqual(updated_step.status, "completed")
            self.assertEqual(updated_step.evidence["tests"], "targeted")
            self.assertEqual([step.status for step in current_steps], ["completed", "in_progress"])
            self.assertEqual(updated_flow.status, "running")
            self.assertEqual(store.get_active_flow("session-1").flow_id, flow.flow_id)  # type: ignore[union-attr]

    def test_failed_flow_step_blocks_but_does_not_fail_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LongTaskContextStore(db_path=Path(tmp) / "tasks.db")
            flow, steps = store.upsert_flow(session_id="session-1", goal="Run migration", steps=["Apply", "Verify"])

            updated_flow, updated_step = store.update_flow_step(
                flow_id=flow.flow_id,
                step_id=steps[0].step_id,
                status="failed",
                last_error="migration failed",
            )

            self.assertEqual(updated_step.status, "failed")
            self.assertEqual(updated_step.last_error, "migration failed")
            self.assertEqual(updated_flow.status, "blocked")
            self.assertIsNone(updated_flow.completed_at_ms)

    def test_finish_task_flow_marks_flow_and_steps_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LongTaskContextStore(db_path=Path(tmp) / "tasks.db")
            flow, _ = store.upsert_flow(session_id="session-1", goal="Prepare report", steps=["Draft", "Review"])

            completed = store.finish_flow(session_id="session-1", flow_id=flow.flow_id, evidence={"artifact": "report.md"})
            steps = store.list_flow_steps(flow_id=flow.flow_id)

            self.assertIsNotNone(completed)
            assert completed is not None
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.evidence["artifact"], "report.md")
            self.assertEqual([step.status for step in steps], ["completed", "completed"])

    def test_context_summary_roundtrip_and_scope_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LongTaskContextStore(db_path=Path(tmp) / "tasks.db")

            first = store.upsert_summary(
                session_id="session-1",
                title="Decision",
                content="Use supervised execution.",
                scope="flow",
                flow_id="flow-1",
                metadata={"source": "test"},
            )
            store.upsert_summary(session_id="session-1", title="Other", content="Other summary", scope="session")

            scoped = store.list_summaries(session_id="session-1", flow_id="flow-1")
            all_summaries = store.list_summaries(session_id="session-1", limit=5)

            self.assertEqual(scoped[0].summary_id, first.summary_id)
            self.assertEqual(scoped[0].metadata["source"], "test")
            self.assertEqual(len(all_summaries), 2)

    def test_summarize_text_is_deterministic_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LongTaskContextStore(db_path=Path(tmp) / "tasks.db")
            source = "A" * 120 + "\n" + "B" * 120 + "\n" + "C" * 120

            summary = store.summarize_text(session_id="session-1", title="Large output", text=source, max_chars=120)

            self.assertLessEqual(len(summary.content), 140)
            self.assertIn("context summary truncated", summary.content)
            self.assertEqual(summary.source_kind, "deterministic")
            self.assertEqual(summary.metadata["source_chars"], len(source))


if __name__ == "__main__":
    unittest.main()
