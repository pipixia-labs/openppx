"""Seed a deterministic TaskRun for the continue/resume ADK eval.

Run this from the repository root with ``OPENPPX_TASK_DB_PATH`` pointing at
the same SQLite database the eval agent will use.
"""

from __future__ import annotations

import os

from openppx.runtime.task_store import TaskStore


EVAL_TASK_ID = "task_continue_eval"
EVAL_SESSION_ID = "continue_eval_session"
EVAL_USER_ID = "eval_continue_user"


def main() -> int:
    """Create or refresh the deterministic continue eval TaskRun."""
    db_path = os.getenv("OPENPPX_TASK_DB_PATH", "").strip()
    if not db_path:
        print("Set OPENPPX_TASK_DB_PATH before running this seed script.")
        return 2

    runner_payload = {
        "runner": "continue_eval_rejoin",
        "seeded_by": "tests/eval/seed_continue_task.py",
    }
    runner_capabilities = {
        "status": False,
        "output": True,
        "rejoin": True,
        "pause": False,
        "checkpoint": False,
    }
    store = TaskStore(db_path=db_path)
    existing = store.get_task(EVAL_TASK_ID)
    if existing is None:
        task = store.create_task(
            task_id=EVAL_TASK_ID,
            kind="eval",
            status="running",
            title="Continue eval rejoinable task",
            owner_key=EVAL_USER_ID,
            user_id=EVAL_USER_ID,
            thread_id=EVAL_SESSION_ID,
            session_id=EVAL_SESSION_ID,
            runner_payload=runner_payload,
            runner_capabilities=runner_capabilities,
            resume_policy="rejoin",
            stop_policy="not_stoppable",
            cancel_policy="unsupported",
            progress_summary="Seeded continue eval task is running and can be rejoined.",
        )
    else:
        task = store.update_task(
            EVAL_TASK_ID,
            status="running",
            runner_payload=runner_payload,
            runner_capabilities=runner_capabilities,
            resume_policy="rejoin",
            stop_policy="not_stoppable",
            cancel_policy="unsupported",
            progress_summary="Seeded continue eval task is running and can be rejoined.",
            terminal_summary="",
            last_error="",
            ended_at_ms=None,
        )
    if task is None:
        print(f"Failed to seed {EVAL_TASK_ID}.")
        return 1
    print(f"Seeded {task.task_id} in session {task.session_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
