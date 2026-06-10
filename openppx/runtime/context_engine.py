"""Minimal context engine facts for long-running user goals."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .task_store import task_db_path


GOAL_ACTIVE_STATUSES = frozenset({"active"})
TODO_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})
FLOW_ACTIVE_STATUSES = frozenset({"planning", "running", "waiting_user", "waiting_approval", "blocked"})
FLOW_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
FLOW_STATUSES = FLOW_ACTIVE_STATUSES | FLOW_TERMINAL_STATUSES
FLOW_STEP_STATUSES = frozenset(
    {"pending", "in_progress", "waiting_user", "waiting_approval", "blocked", "completed", "failed", "cancelled"}
)
DEFAULT_SUMMARY_MAX_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class GoalMirror:
    """A short-term goal mirror injected into model context."""

    goal_id: str
    session_id: str
    status: str
    objective: str
    completion_criteria: str
    current_summary: str
    created_at_ms: int
    updated_at_ms: int
    completed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class TodoItem:
    """A short-term todo item for one goal/session."""

    todo_id: str
    goal_id: str
    session_id: str
    order_index: int
    content: str
    status: str
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class TaskFlow:
    """A durable multi-step user-goal fact, separate from TaskRun execution."""

    flow_id: str
    session_id: str
    goal_id: str
    status: str
    sync_mode: str
    goal: str
    current_step_id: str
    blocked_task_id: str
    wait_payload: dict[str, Any]
    evidence: dict[str, Any]
    revision: int
    created_at_ms: int
    updated_at_ms: int
    completed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class TaskFlowStep:
    """One ordered step inside a TaskFlow."""

    step_id: str
    flow_id: str
    session_id: str
    order_index: int
    title: str
    status: str
    task_id: str
    depends_on: tuple[str, ...]
    evidence: dict[str, Any]
    last_error: str
    created_at_ms: int
    updated_at_ms: int
    completed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class ContextSummary:
    """A compact staged summary fact for long-running context continuity."""

    summary_id: str
    session_id: str
    scope: str
    goal_id: str
    flow_id: str
    task_id: str
    title: str
    content: str
    source_kind: str
    metadata: dict[str, Any]
    created_at_ms: int
    updated_at_ms: int


class LongTaskContextStore:
    """SQLite store for short-term goal mirrors and todos.

    These facts are not long-term memory and are not runner state. They exist to
    keep the current user goal, completion criteria, and immediate todo list in
    the model's recent context during long multi-turn work.
    """

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path).expanduser() if db_path is not None else task_db_path()
        self._lock = threading.Lock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create context-engine tables when missing."""
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS long_task_goals (
                    goal_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    completion_criteria TEXT NOT NULL,
                    current_summary TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    completed_at_ms INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS long_task_todos (
                    todo_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    order_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS long_task_flows (
                    flow_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    goal_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sync_mode TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    current_step_id TEXT NOT NULL,
                    blocked_task_id TEXT NOT NULL,
                    wait_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    completed_at_ms INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS long_task_flow_steps (
                    step_id TEXT PRIMARY KEY,
                    flow_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    order_index INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    depends_on_json TEXT NOT NULL DEFAULT '[]',
                    evidence_json TEXT NOT NULL,
                    last_error TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    completed_at_ms INTEGER
                )
                """
            )
            _ensure_columns(
                conn,
                "long_task_flow_steps",
                {
                    "depends_on_json": "TEXT NOT NULL DEFAULT '[]'",
                },
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS long_task_context_summaries (
                    summary_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    goal_id TEXT NOT NULL,
                    flow_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_long_task_goals_session_status "
                "ON long_task_goals(session_id, status, updated_at_ms)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_long_task_todos_goal_order "
                "ON long_task_todos(goal_id, order_index)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_long_task_todos_session_order "
                "ON long_task_todos(session_id, order_index)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_long_task_flows_session_status "
                "ON long_task_flows(session_id, status, updated_at_ms)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_long_task_flow_steps_flow_order "
                "ON long_task_flow_steps(flow_id, order_index)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_long_task_context_summaries_session "
                "ON long_task_context_summaries(session_id, updated_at_ms)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_long_task_context_summaries_flow "
                "ON long_task_context_summaries(flow_id, updated_at_ms)"
            )

    def upsert_goal(
        self,
        *,
        session_id: str,
        objective: str,
        completion_criteria: str = "",
        current_summary: str = "",
        goal_id: str | None = None,
    ) -> GoalMirror:
        """Create or update the active goal mirror for one session."""
        normalized_session = _text(session_id)
        normalized_goal_id = _text(goal_id)
        existing = self.get_goal(normalized_goal_id) if normalized_goal_id else self.get_active_goal(normalized_session)
        now_ms = _now_ms()
        normalized_objective = _text(objective) or (existing.objective if existing is not None else "")
        if not normalized_objective:
            raise ValueError("objective is required")
        if existing is None:
            resolved_goal_id = normalized_goal_id or f"goal_{uuid.uuid4().hex[:16]}"
            with self._lock, _connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO long_task_goals (
                        goal_id, session_id, status, objective, completion_criteria,
                        current_summary, created_at_ms, updated_at_ms, completed_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_goal_id,
                        normalized_session,
                        "active",
                        normalized_objective,
                        _text(completion_criteria),
                        _text(current_summary),
                        now_ms,
                        now_ms,
                        None,
                    ),
                )
            goal = self.get_goal(resolved_goal_id)
            assert goal is not None
            return goal

        with self._lock, _connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE long_task_goals
                SET status = 'active',
                    objective = ?,
                    completion_criteria = ?,
                    current_summary = ?,
                    updated_at_ms = ?,
                    completed_at_ms = NULL
                WHERE goal_id = ?
                """,
                (
                    normalized_objective,
                    _text(completion_criteria) or existing.completion_criteria,
                    _text(current_summary) or existing.current_summary,
                    now_ms,
                    existing.goal_id,
                ),
            )
        goal = self.get_goal(existing.goal_id)
        assert goal is not None
        return goal

    def get_goal(self, goal_id: str) -> GoalMirror | None:
        """Return one goal mirror by id."""
        normalized = _text(goal_id)
        if not normalized:
            return None
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM long_task_goals WHERE goal_id = ?", (normalized,)).fetchone()
        return _goal_from_row(row) if row is not None else None

    def get_active_goal(self, session_id: str) -> GoalMirror | None:
        """Return the most recently updated active goal for a session."""
        normalized_session = _text(session_id)
        if not normalized_session:
            return None
        with _connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM long_task_goals
                WHERE session_id = ? AND status IN ('active')
                ORDER BY updated_at_ms DESC
                LIMIT 1
                """,
                (normalized_session,),
            ).fetchone()
        return _goal_from_row(row) if row is not None else None

    def complete_goal(self, *, session_id: str, goal_id: str | None = None, final_summary: str = "") -> GoalMirror | None:
        """Mark one goal mirror completed without touching long-term memory."""
        goal = self.get_goal(_text(goal_id)) if _text(goal_id) else self.get_active_goal(session_id)
        if goal is None:
            return None
        now_ms = _now_ms()
        summary = _text(final_summary) or goal.current_summary
        with self._lock, _connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE long_task_goals
                SET status = 'completed',
                    current_summary = ?,
                    updated_at_ms = ?,
                    completed_at_ms = ?
                WHERE goal_id = ?
                """,
                (summary, now_ms, now_ms, goal.goal_id),
            )
            conn.execute(
                """
                UPDATE long_task_todos
                SET status = 'completed', updated_at_ms = ?
                WHERE goal_id = ? AND status != 'cancelled'
                """,
                (now_ms, goal.goal_id),
            )
        return self.get_goal(goal.goal_id)

    def replace_todos(
        self,
        *,
        session_id: str,
        items: Any,
        goal_id: str | None = None,
    ) -> list[TodoItem]:
        """Replace the current todo list for a session or active goal."""
        normalized_session = _text(session_id)
        goal = self.get_goal(_text(goal_id)) if _text(goal_id) else self.get_active_goal(normalized_session)
        resolved_goal_id = goal.goal_id if goal is not None else ""
        normalized_items = _normalize_todo_items(items)
        now_ms = _now_ms()
        with self._lock, _connect(self.db_path) as conn:
            if resolved_goal_id:
                conn.execute("DELETE FROM long_task_todos WHERE goal_id = ?", (resolved_goal_id,))
            else:
                conn.execute("DELETE FROM long_task_todos WHERE session_id = ? AND goal_id = ''", (normalized_session,))
            for index, item in enumerate(normalized_items):
                conn.execute(
                    """
                    INSERT INTO long_task_todos (
                        todo_id, goal_id, session_id, order_index, content,
                        status, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"todo_{uuid.uuid4().hex[:16]}",
                        resolved_goal_id,
                        normalized_session,
                        index,
                        item["content"],
                        item["status"],
                        now_ms,
                        now_ms,
                    ),
                )
        return self.list_todos(session_id=normalized_session, goal_id=resolved_goal_id or None)

    def list_todos(
        self,
        *,
        session_id: str,
        goal_id: str | None = None,
        limit: int = 20,
    ) -> list[TodoItem]:
        """List todo items for a goal or session."""
        normalized_goal_id = _text(goal_id)
        normalized_session = _text(session_id)
        safe_limit = max(1, min(int(limit or 20), 100))
        if normalized_goal_id:
            where = "goal_id = ?"
            params: tuple[Any, ...] = (normalized_goal_id, safe_limit)
        else:
            where = "session_id = ? AND goal_id = ''"
            params = (normalized_session, safe_limit)
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM long_task_todos
                WHERE {where}
                ORDER BY order_index ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_todo_from_row(row) for row in rows]

    def upsert_flow(
        self,
        *,
        session_id: str,
        goal: str,
        steps: Any = None,
        flow_id: str | None = None,
        goal_id: str | None = None,
        status: str = "running",
        sync_mode: str = "managed",
        blocked_task_id: str = "",
        wait_payload: Any = None,
        evidence: Any = None,
    ) -> tuple[TaskFlow, list[TaskFlowStep]]:
        """Create or update the active TaskFlow for a session."""
        normalized_session = _text(session_id)
        normalized_flow_id = _text(flow_id)
        existing = self.get_flow(normalized_flow_id) if normalized_flow_id else self.get_active_flow(normalized_session)
        normalized_goal = _text(goal) or (existing.goal if existing is not None else "")
        if not normalized_goal:
            raise ValueError("goal is required")
        normalized_goal_id = _text(goal_id) or (existing.goal_id if existing is not None else "")
        normalized_status = _normalize_flow_status(status)
        normalized_sync_mode = _normalize_sync_mode(sync_mode)
        now_ms = _now_ms()
        if existing is None:
            resolved_flow_id = normalized_flow_id or f"flow_{uuid.uuid4().hex[:16]}"
            with self._lock, _connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO long_task_flows (
                        flow_id, session_id, goal_id, status, sync_mode, goal,
                        current_step_id, blocked_task_id, wait_json, evidence_json,
                        revision, created_at_ms, updated_at_ms, completed_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_flow_id,
                        normalized_session,
                        normalized_goal_id,
                        normalized_status,
                        normalized_sync_mode,
                        normalized_goal,
                        "",
                        _text(blocked_task_id),
                        _json_dumps(_parse_json_object(wait_payload)),
                        _json_dumps(_parse_json_object(evidence)),
                        1,
                        now_ms,
                        now_ms,
                        None,
                    ),
                )
        else:
            resolved_flow_id = existing.flow_id
            with self._lock, _connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE long_task_flows
                    SET goal_id = ?,
                        status = ?,
                        sync_mode = ?,
                        goal = ?,
                        blocked_task_id = ?,
                        wait_json = ?,
                        evidence_json = ?,
                        revision = revision + 1,
                        updated_at_ms = ?,
                        completed_at_ms = CASE
                            WHEN ? IN ('completed', 'failed', 'cancelled') THEN COALESCE(completed_at_ms, ?)
                            ELSE NULL
                        END
                    WHERE flow_id = ?
                    """,
                    (
                        normalized_goal_id,
                        normalized_status,
                        normalized_sync_mode,
                        normalized_goal,
                        _text(blocked_task_id) or existing.blocked_task_id,
                        _json_dumps(_parse_json_object(wait_payload) or existing.wait_payload),
                        _json_dumps(_parse_json_object(evidence) or existing.evidence),
                        now_ms,
                        normalized_status,
                        now_ms,
                        resolved_flow_id,
                    ),
                )
        if steps is not None:
            self.replace_flow_steps(session_id=normalized_session, flow_id=resolved_flow_id, steps=steps)
        self._refresh_flow_current_step(resolved_flow_id)
        flow = self.get_flow(resolved_flow_id)
        assert flow is not None
        return flow, self.list_flow_steps(flow_id=resolved_flow_id)

    def get_flow(self, flow_id: str) -> TaskFlow | None:
        """Return one TaskFlow by id."""
        normalized = _text(flow_id)
        if not normalized:
            return None
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM long_task_flows WHERE flow_id = ?", (normalized,)).fetchone()
        return _flow_from_row(row) if row is not None else None

    def get_active_flow(self, session_id: str) -> TaskFlow | None:
        """Return the most recently updated non-terminal TaskFlow for a session."""
        normalized_session = _text(session_id)
        if not normalized_session:
            return None
        placeholders = ", ".join("?" for _ in FLOW_ACTIVE_STATUSES)
        with _connect(self.db_path) as conn:
            row = conn.execute(
                f"""
                SELECT * FROM long_task_flows
                WHERE session_id = ? AND status IN ({placeholders})
                ORDER BY updated_at_ms DESC
                LIMIT 1
                """,
                (normalized_session, *sorted(FLOW_ACTIVE_STATUSES)),
            ).fetchone()
        return _flow_from_row(row) if row is not None else None

    def list_flows(
        self,
        *,
        session_id: str,
        statuses: Iterable[str] | None = None,
        limit: int = 10,
    ) -> list[TaskFlow]:
        """List recent TaskFlow facts for one session."""
        normalized_session = _text(session_id)
        if not normalized_session:
            return []
        safe_limit = max(1, min(int(limit or 10), 100))
        normalized_statuses = [_normalize_flow_status(status) for status in (statuses or FLOW_ACTIVE_STATUSES)]
        placeholders = ", ".join("?" for _ in normalized_statuses)
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM long_task_flows
                WHERE session_id = ? AND status IN ({placeholders})
                ORDER BY updated_at_ms DESC
                LIMIT ?
                """,
                (normalized_session, *normalized_statuses, safe_limit),
            ).fetchall()
        return [_flow_from_row(row) for row in rows]

    def replace_flow_steps(self, *, session_id: str, flow_id: str, steps: Any) -> list[TaskFlowStep]:
        """Replace ordered steps for one TaskFlow."""
        normalized_flow_id = _text(flow_id)
        if self.get_flow(normalized_flow_id) is None:
            raise ValueError("flow not found")
        normalized_steps = _normalize_flow_steps(steps)
        now_ms = _now_ms()
        with self._lock, _connect(self.db_path) as conn:
            conn.execute("DELETE FROM long_task_flow_steps WHERE flow_id = ?", (normalized_flow_id,))
            for index, step in enumerate(normalized_steps):
                status = step["status"]
                completed_at_ms = now_ms if status in {"completed", "failed", "cancelled"} else None
                conn.execute(
                    """
                    INSERT INTO long_task_flow_steps (
                        step_id, flow_id, session_id, order_index, title, status,
                        task_id, depends_on_json, evidence_json, last_error, created_at_ms,
                        updated_at_ms, completed_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        step["step_id"],
                        normalized_flow_id,
                        _text(session_id),
                        index,
                        step["title"],
                        status,
                        step["task_id"],
                        _json_dumps(list(step["depends_on"])),
                        _json_dumps(step["evidence"]),
                        step["last_error"],
                        now_ms,
                        now_ms,
                        completed_at_ms,
                    ),
                )
            conn.execute(
                """
                UPDATE long_task_flows
                SET revision = revision + 1, updated_at_ms = ?
                WHERE flow_id = ?
                """,
                (now_ms, normalized_flow_id),
            )
        self._refresh_flow_current_step(normalized_flow_id)
        return self.list_flow_steps(flow_id=normalized_flow_id)

    def list_flow_steps(self, *, flow_id: str, limit: int = 50) -> list[TaskFlowStep]:
        """List ordered steps for one TaskFlow."""
        normalized_flow_id = _text(flow_id)
        if not normalized_flow_id:
            return []
        safe_limit = max(1, min(int(limit or 50), 200))
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM long_task_flow_steps
                WHERE flow_id = ?
                ORDER BY order_index ASC
                LIMIT ?
                """,
                (normalized_flow_id, safe_limit),
            ).fetchall()
        return [_flow_step_from_row(row) for row in rows]

    def update_flow_step(
        self,
        *,
        flow_id: str,
        step_id: str | None = None,
        order_index: int | None = None,
        status: str | None = None,
        task_id: str = "",
        depends_on: Any = None,
        evidence: Any = None,
        last_error: str = "",
    ) -> tuple[TaskFlow, TaskFlowStep]:
        """Update one TaskFlow step fact without executing the step."""
        flow = self.get_flow(flow_id)
        if flow is None:
            raise ValueError("flow not found")
        step = self._find_flow_step(flow_id=flow.flow_id, step_id=step_id, order_index=order_index)
        if step is None:
            raise ValueError("flow step not found")
        normalized_status = _normalize_flow_step_status(status or step.status)
        now_ms = _now_ms()
        completed_at_ms = now_ms if normalized_status in {"completed", "failed", "cancelled"} else None
        evidence_payload = _parse_json_object(evidence) or step.evidence
        task_ref = _text(task_id) or step.task_id
        current_steps = self.list_flow_steps(flow_id=flow.flow_id, limit=200)
        dependency_refs = _normalize_flow_dependency_refs(depends_on)
        resolved_depends_on = (
            _resolve_flow_dependency_refs(
                [
                    {
                        "step_id": candidate.step_id,
                        "title": candidate.title,
                        "depends_on": (),
                    }
                    for candidate in current_steps
                ],
                dependency_refs,
                current_step_id=step.step_id,
            )
            if depends_on is not None
            else step.depends_on
        )
        if depends_on is not None:
            _validate_flow_dependency_graph(
                [
                    {
                        "step_id": candidate.step_id,
                        "depends_on": resolved_depends_on if candidate.step_id == step.step_id else candidate.depends_on,
                    }
                    for candidate in current_steps
                ]
            )
        error_text = _text(last_error) or step.last_error
        with self._lock, _connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE long_task_flow_steps
                SET status = ?,
                    task_id = ?,
                    depends_on_json = ?,
                    evidence_json = ?,
                    last_error = ?,
                    updated_at_ms = ?,
                    completed_at_ms = ?
                WHERE step_id = ?
                """,
                (
                    normalized_status,
                    task_ref,
                    _json_dumps(list(resolved_depends_on)),
                    _json_dumps(evidence_payload),
                    error_text,
                    now_ms,
                    completed_at_ms,
                    step.step_id,
                ),
            )
            flow_status = _flow_status_after_step_update(flow.status, normalized_status)
            conn.execute(
                """
                UPDATE long_task_flows
                SET status = ?,
                    current_step_id = ?,
                    blocked_task_id = ?,
                    revision = revision + 1,
                    updated_at_ms = ?,
                    completed_at_ms = CASE
                        WHEN ? IN ('completed', 'failed', 'cancelled') THEN COALESCE(completed_at_ms, ?)
                        ELSE completed_at_ms
                    END
                WHERE flow_id = ?
                """,
                (
                    flow_status,
                    step.step_id,
                    task_ref if flow_status == "blocked" else flow.blocked_task_id,
                    now_ms,
                    flow_status,
                    now_ms,
                    flow.flow_id,
                ),
            )
        self._refresh_flow_current_step(flow.flow_id)
        updated_flow = self.get_flow(flow.flow_id)
        updated_step = self._find_flow_step(flow_id=flow.flow_id, step_id=step.step_id, order_index=None)
        assert updated_flow is not None
        assert updated_step is not None
        return updated_flow, updated_step

    def project_flow(self, *, flow_id: str) -> dict[str, Any]:
        """Return deterministic DAG execution state for one TaskFlow."""
        flow = self.get_flow(flow_id)
        if flow is None:
            raise ValueError("flow not found")
        steps = self.list_flow_steps(flow_id=flow.flow_id, limit=200)
        return _flow_execution_projection(flow, steps)

    def advance_flow(
        self,
        *,
        session_id: str,
        flow_id: str | None = None,
        auto_finish: bool = True,
        start_ready: bool = True,
    ) -> tuple[TaskFlow | None, list[TaskFlowStep], dict[str, Any]]:
        """Advance TaskFlow DAG bookkeeping without executing external work."""
        flow = self.get_flow(_text(flow_id)) if _text(flow_id) else self.get_active_flow(session_id)
        if flow is None:
            return None, [], {}
        if start_ready:
            self._refresh_flow_current_step(flow.flow_id)
        steps = self.list_flow_steps(flow_id=flow.flow_id, limit=200)
        projection = _flow_execution_projection(flow, steps)
        if auto_finish and projection["all_steps_completed"] and flow.status not in FLOW_TERMINAL_STATUSES:
            flow = self.finish_flow(session_id=flow.session_id, flow_id=flow.flow_id, status="completed")
            assert flow is not None
            steps = self.list_flow_steps(flow_id=flow.flow_id, limit=200)
            projection = _flow_execution_projection(flow, steps)
        elif projection["has_dependency_blocker"] and flow.status not in FLOW_TERMINAL_STATUSES:
            now_ms = _now_ms()
            blocked_task_id = ""
            first_blocked = next((step for step in steps if step.step_id in projection["blocked_step_ids"]), None)
            if first_blocked is not None:
                blocked_task_id = first_blocked.task_id
            with self._lock, _connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE long_task_flows
                    SET status = 'blocked',
                        blocked_task_id = ?,
                        revision = revision + 1,
                        updated_at_ms = ?
                    WHERE flow_id = ?
                    """,
                    (blocked_task_id or flow.blocked_task_id, now_ms, flow.flow_id),
                )
            flow = self.get_flow(flow.flow_id)
            assert flow is not None
            projection = _flow_execution_projection(flow, steps)
        return flow, steps, projection

    def finish_flow(
        self,
        *,
        session_id: str,
        flow_id: str | None = None,
        status: str = "completed",
        evidence: Any = None,
    ) -> TaskFlow | None:
        """Mark one TaskFlow terminal without restarting or executing work."""
        flow = self.get_flow(_text(flow_id)) if _text(flow_id) else self.get_active_flow(session_id)
        if flow is None:
            return None
        normalized_status = _normalize_flow_terminal_status(status)
        now_ms = _now_ms()
        with self._lock, _connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE long_task_flows
                SET status = ?,
                    evidence_json = ?,
                    revision = revision + 1,
                    updated_at_ms = ?,
                    completed_at_ms = ?
                WHERE flow_id = ?
                """,
                (
                    normalized_status,
                    _json_dumps(_parse_json_object(evidence) or flow.evidence),
                    now_ms,
                    now_ms,
                    flow.flow_id,
                ),
            )
            if normalized_status == "completed":
                conn.execute(
                    """
                    UPDATE long_task_flow_steps
                    SET status = 'completed', updated_at_ms = ?, completed_at_ms = ?
                    WHERE flow_id = ? AND status NOT IN ('completed', 'cancelled')
                    """,
                    (now_ms, now_ms, flow.flow_id),
                )
            elif normalized_status == "cancelled":
                conn.execute(
                    """
                    UPDATE long_task_flow_steps
                    SET status = 'cancelled', updated_at_ms = ?, completed_at_ms = ?
                    WHERE flow_id = ? AND status NOT IN ('completed', 'failed', 'cancelled')
                    """,
                    (now_ms, now_ms, flow.flow_id),
                )
        return self.get_flow(flow.flow_id)

    def upsert_summary(
        self,
        *,
        session_id: str,
        content: str,
        title: str = "",
        summary_id: str | None = None,
        scope: str = "session",
        goal_id: str | None = None,
        flow_id: str | None = None,
        task_id: str | None = None,
        source_kind: str = "manual",
        metadata: Any = None,
        max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    ) -> ContextSummary:
        """Create or update one staged context summary fact."""
        normalized_session = _text(session_id)
        normalized_content = _truncate_text(_text(content), max_chars=max(1, int(max_chars or DEFAULT_SUMMARY_MAX_CHARS)))
        if not normalized_content:
            raise ValueError("content is required")
        normalized_summary_id = _text(summary_id)
        resolved_summary_id = normalized_summary_id or f"summary_{uuid.uuid4().hex[:16]}"
        now_ms = _now_ms()
        with self._lock, _connect(self.db_path) as conn:
            existing = conn.execute(
                "SELECT summary_id FROM long_task_context_summaries WHERE summary_id = ?",
                (resolved_summary_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO long_task_context_summaries (
                        summary_id, session_id, scope, goal_id, flow_id, task_id,
                        title, content, source_kind, metadata_json,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_summary_id,
                        normalized_session,
                        _normalize_summary_scope(scope),
                        _text(goal_id),
                        _text(flow_id),
                        _text(task_id),
                        _text(title),
                        normalized_content,
                        _text(source_kind) or "manual",
                        _json_dumps(_parse_json_object(metadata)),
                        now_ms,
                        now_ms,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE long_task_context_summaries
                    SET scope = ?,
                        goal_id = ?,
                        flow_id = ?,
                        task_id = ?,
                        title = ?,
                        content = ?,
                        source_kind = ?,
                        metadata_json = ?,
                        updated_at_ms = ?
                    WHERE summary_id = ?
                    """,
                    (
                        _normalize_summary_scope(scope),
                        _text(goal_id),
                        _text(flow_id),
                        _text(task_id),
                        _text(title),
                        normalized_content,
                        _text(source_kind) or "manual",
                        _json_dumps(_parse_json_object(metadata)),
                        now_ms,
                        resolved_summary_id,
                    ),
                )
        summary = self.get_summary(resolved_summary_id)
        assert summary is not None
        return summary

    def get_summary(self, summary_id: str) -> ContextSummary | None:
        """Return one staged context summary by id."""
        normalized = _text(summary_id)
        if not normalized:
            return None
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM long_task_context_summaries WHERE summary_id = ?",
                (normalized,),
            ).fetchone()
        return _summary_from_row(row) if row is not None else None

    def list_summaries(
        self,
        *,
        session_id: str,
        goal_id: str | None = None,
        flow_id: str | None = None,
        task_id: str | None = None,
        scope: str | None = None,
        limit: int = 5,
    ) -> list[ContextSummary]:
        """List recent staged summaries for one session and optional scope."""
        normalized_session = _text(session_id)
        if not normalized_session:
            return []
        safe_limit = max(1, min(int(limit or 5), 50))
        clauses = ["session_id = ?"]
        params: list[Any] = [normalized_session]
        if _text(goal_id):
            clauses.append("goal_id = ?")
            params.append(_text(goal_id))
        if _text(flow_id):
            clauses.append("flow_id = ?")
            params.append(_text(flow_id))
        if _text(task_id):
            clauses.append("task_id = ?")
            params.append(_text(task_id))
        normalized_scope = _normalize_optional_summary_scope(scope)
        if normalized_scope:
            clauses.append("scope = ?")
            params.append(normalized_scope)
        params.append(safe_limit)
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM long_task_context_summaries
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at_ms DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_summary_from_row(row) for row in rows]

    def rollup_summaries(
        self,
        *,
        session_id: str,
        target_scope: str = "session",
        source_scope: str | None = None,
        title: str = "",
        summary_id: str | None = None,
        goal_id: str | None = None,
        flow_id: str | None = None,
        task_id: str | None = None,
        limit: int = 20,
        max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    ) -> ContextSummary:
        """Create a deterministic higher-level summary from lower-level summaries."""
        normalized_session = _text(session_id)
        if not normalized_session:
            raise ValueError("session_id is required")
        normalized_target_scope = _normalize_summary_scope(target_scope)
        normalized_source_scope = _normalize_optional_summary_scope(source_scope)
        safe_limit = max(1, min(int(limit or 20), 100))
        sources = self._collect_rollup_sources(
            session_id=normalized_session,
            source_scope=normalized_source_scope,
            goal_id=goal_id,
            flow_id=flow_id,
            task_id=task_id,
            limit=safe_limit,
            exclude_summary_id=summary_id,
        )
        if not sources:
            raise ValueError("no context summaries available for rollup")
        content = _rollup_summary_content(sources, max_chars=max_chars)
        metadata = {
            "rollup_version": 1,
            "target_scope": normalized_target_scope,
            "source_scope": normalized_source_scope or "any",
            "source_count": len(sources),
            "source_summary_ids": [source.summary_id for source in sources],
        }
        return self.upsert_summary(
            session_id=normalized_session,
            content=content,
            title=_text(title) or f"{normalized_target_scope.title()} context rollup",
            summary_id=summary_id,
            scope=normalized_target_scope,
            goal_id=goal_id if normalized_target_scope in {"goal", "flow", "task"} else "",
            flow_id=flow_id if normalized_target_scope in {"flow", "task"} else "",
            task_id=task_id if normalized_target_scope == "task" else "",
            source_kind="summary_rollup",
            metadata=metadata,
            max_chars=max_chars,
        )

    def _collect_rollup_sources(
        self,
        *,
        session_id: str,
        source_scope: str,
        goal_id: str | None,
        flow_id: str | None,
        task_id: str | None,
        limit: int,
        exclude_summary_id: str | None,
    ) -> list[ContextSummary]:
        """Collect unique source summaries for deterministic rollup."""
        seen: dict[str, ContextSummary] = {}

        def add(items: Iterable[ContextSummary]) -> None:
            for item in items:
                if _text(exclude_summary_id) and item.summary_id == _text(exclude_summary_id):
                    continue
                if source_scope and item.scope != source_scope:
                    continue
                seen[item.summary_id] = item

        if _text(task_id):
            add(self.list_summaries(session_id=session_id, task_id=task_id, scope=source_scope or None, limit=limit))
        if _text(flow_id):
            add(self.list_summaries(session_id=session_id, flow_id=flow_id, scope=source_scope or None, limit=limit))
            for step in self.list_flow_steps(flow_id=_text(flow_id), limit=200):
                if step.task_id:
                    add(
                        self.list_summaries(
                            session_id=session_id,
                            task_id=step.task_id,
                            scope=source_scope or None,
                            limit=limit,
                        )
                    )
        if _text(goal_id):
            add(self.list_summaries(session_id=session_id, goal_id=goal_id, scope=source_scope or None, limit=limit))
        if not (_text(task_id) or _text(flow_id) or _text(goal_id)):
            add(self.list_summaries(session_id=session_id, scope=source_scope or None, limit=limit))
        return sorted(seen.values(), key=lambda item: item.updated_at_ms, reverse=True)[:limit]

    def summarize_text(
        self,
        *,
        session_id: str,
        text: str,
        title: str = "",
        summary_id: str | None = None,
        scope: str = "session",
        goal_id: str | None = None,
        flow_id: str | None = None,
        task_id: str | None = None,
        max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    ) -> ContextSummary:
        """Store a deterministic compact summary extracted from supplied text."""
        content = _compact_text(text, max_chars=max_chars)
        return self.upsert_summary(
            session_id=session_id,
            content=content,
            title=title,
            summary_id=summary_id,
            scope=scope,
            goal_id=goal_id,
            flow_id=flow_id,
            task_id=task_id,
            source_kind="deterministic",
            metadata={"source_chars": len(str(text or "")), "max_chars": max_chars},
            max_chars=max(len(content), max_chars),
        )

    def _find_flow_step(
        self,
        *,
        flow_id: str,
        step_id: str | None,
        order_index: int | None,
    ) -> TaskFlowStep | None:
        normalized_flow_id = _text(flow_id)
        normalized_step_id = _text(step_id)
        with _connect(self.db_path) as conn:
            if normalized_step_id:
                row = conn.execute(
                    "SELECT * FROM long_task_flow_steps WHERE flow_id = ? AND step_id = ?",
                    (normalized_flow_id, normalized_step_id),
                ).fetchone()
            elif order_index is not None:
                row = conn.execute(
                    "SELECT * FROM long_task_flow_steps WHERE flow_id = ? AND order_index = ?",
                    (normalized_flow_id, int(order_index)),
                ).fetchone()
            else:
                row = None
        return _flow_step_from_row(row) if row is not None else None

    def _refresh_flow_current_step(self, flow_id: str) -> None:
        normalized_flow_id = _text(flow_id)
        if not normalized_flow_id:
            return
        flow = self.get_flow(normalized_flow_id)
        steps = self.list_flow_steps(flow_id=normalized_flow_id, limit=200)
        current_step_id = ""
        projection = _flow_execution_projection(flow, steps) if flow is not None else {}
        active_step_ids = set(projection.get("active_step_ids", []))
        if flow is not None and flow.status not in FLOW_TERMINAL_STATUSES and not active_step_ids:
            ready_step_ids = set(projection.get("ready_step_ids", []))
            next_pending = next((step for step in steps if step.status == "pending" and step.step_id in ready_step_ids), None)
            if next_pending is not None:
                now_ms = _now_ms()
                with self._lock, _connect(self.db_path) as conn:
                    conn.execute(
                        """
                        UPDATE long_task_flow_steps
                        SET status = 'in_progress', updated_at_ms = ?
                        WHERE step_id = ?
                        """,
                        (now_ms, next_pending.step_id),
                    )
                steps = self.list_flow_steps(flow_id=normalized_flow_id, limit=200)
                projection = _flow_execution_projection(flow, steps)
                active_step_ids = set(projection.get("active_step_ids", []))
        for status_group in (
            {"in_progress", "waiting_user", "waiting_approval", "blocked", "failed"},
            {"pending"},
        ):
            match = next(
                (
                    step
                    for step in steps
                    if step.status in status_group
                    and (step.step_id in active_step_ids or status_group == {"pending"})
                ),
                None,
            )
            if match is not None:
                current_step_id = match.step_id
                break
        with self._lock, _connect(self.db_path) as conn:
            conn.execute(
                "UPDATE long_task_flows SET current_step_id = ? WHERE flow_id = ?",
                (current_step_id, normalized_flow_id),
            )


def _normalize_todo_items(items: Any) -> list[dict[str, str]]:
    """Normalize loose tool input into todo rows with at most one in-progress item."""
    parsed = _parse_items(items)
    normalized: list[dict[str, str]] = []
    for item in parsed:
        if isinstance(item, str):
            content = _text(item)
            status = "pending"
        elif isinstance(item, dict):
            content = _text(item.get("content") or item.get("text") or item.get("task"))
            status = _normalize_todo_status(item.get("status"))
        else:
            continue
        if content:
            normalized.append({"content": content, "status": status})
    in_progress_seen = False
    first_pending = -1
    for index, item in enumerate(normalized):
        if item["status"] == "in_progress":
            if in_progress_seen:
                item["status"] = "pending"
            in_progress_seen = True
        elif item["status"] == "pending" and first_pending < 0:
            first_pending = index
    if not in_progress_seen and first_pending >= 0:
        normalized[first_pending]["status"] = "in_progress"
    return normalized


def _normalize_flow_steps(items: Any) -> list[dict[str, Any]]:
    """Normalize loose tool input into ordered flow step rows."""
    parsed = _parse_items(items)
    normalized: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, str):
            step_id = f"step_{uuid.uuid4().hex[:16]}"
            title = _text(item)
            status = "pending"
            task_id = ""
            raw_depends_on: tuple[Any, ...] = ()
            evidence: dict[str, Any] = {}
            last_error = ""
        elif isinstance(item, dict):
            step_id = _text(item.get("step_id")) or f"step_{uuid.uuid4().hex[:16]}"
            title = _text(item.get("title") or item.get("content") or item.get("text") or item.get("task"))
            status = _normalize_flow_step_status(item.get("status"))
            task_id = _text(item.get("task_id"))
            raw_depends_on = _normalize_flow_dependency_refs(
                item.get("depends_on") if "depends_on" in item else item.get("dependsOn", item.get("dependencies"))
            )
            evidence = _parse_json_object(item.get("evidence"))
            last_error = _text(item.get("last_error") or item.get("error"))
        else:
            continue
        if title:
            normalized.append(
                {
                    "step_id": step_id,
                    "title": title,
                    "status": status,
                    "task_id": task_id,
                    "raw_depends_on": raw_depends_on,
                    "depends_on": (),
                    "evidence": evidence,
                    "last_error": last_error,
                }
            )
    for item in normalized:
        item["depends_on"] = _resolve_flow_dependency_refs(
            normalized,
            item.pop("raw_depends_on", ()),
            current_step_id=item["step_id"],
        )
    _validate_flow_dependency_graph(normalized)
    in_progress_seen = False
    first_pending = -1
    for index, item in enumerate(normalized):
        if item["status"] == "in_progress":
            if in_progress_seen:
                item["status"] = "pending"
            in_progress_seen = True
        elif item["status"] == "pending" and first_pending < 0:
            dependencies = tuple(item.get("depends_on") or ())
            if not dependencies:
                first_pending = index
    if not in_progress_seen and first_pending >= 0:
        normalized[first_pending]["status"] = "in_progress"
    return normalized


def _normalize_flow_dependency_refs(value: Any) -> tuple[Any, ...]:
    """Normalize loose dependency input while preserving resolvable ref types."""
    if value is None:
        return ()
    if isinstance(value, (str, int)):
        raw_items: list[Any] = [value]
    elif isinstance(value, dict):
        raw_items = [value]
    elif isinstance(value, Iterable):
        raw_items = list(value)
    else:
        raw_items = [value]
    normalized: list[Any] = []
    for item in raw_items:
        if isinstance(item, dict):
            if "step_id" in item:
                ref: Any = _text(item.get("step_id"))
            elif "order_index" in item:
                ref = item.get("order_index")
            elif "index" in item:
                ref = item.get("index")
            elif "title" in item:
                ref = _text(item.get("title"))
            else:
                ref = ""
        else:
            ref = item
        if isinstance(ref, int):
            normalized.append(ref)
        else:
            text = _text(ref)
            if text:
                normalized.append(text)
    return tuple(normalized)


def _resolve_flow_dependency_refs(
    steps: list[dict[str, Any]],
    refs: Iterable[Any],
    *,
    current_step_id: str,
) -> tuple[str, ...]:
    """Resolve dependency refs to step ids when possible."""
    by_step_id = {_text(step.get("step_id")): _text(step.get("step_id")) for step in steps if _text(step.get("step_id"))}
    title_counts: dict[str, int] = {}
    by_title: dict[str, str] = {}
    for step in steps:
        title = _text(step.get("title"))
        if not title:
            continue
        title_counts[title] = title_counts.get(title, 0) + 1
        by_title[title] = _text(step.get("step_id"))
    resolved: list[str] = []
    for ref in refs:
        dependency_id = ""
        if isinstance(ref, int):
            if 0 <= ref < len(steps):
                dependency_id = _text(steps[ref].get("step_id"))
        else:
            text = _text(ref)
            if text.isdigit():
                index = int(text)
                if 0 <= index < len(steps):
                    dependency_id = _text(steps[index].get("step_id"))
            if not dependency_id and text in by_step_id:
                dependency_id = by_step_id[text]
            if not dependency_id and title_counts.get(text) == 1:
                dependency_id = by_title[text]
            if not dependency_id:
                dependency_id = text
        if dependency_id and dependency_id != current_step_id and dependency_id not in resolved:
            resolved.append(dependency_id)
    return tuple(resolved)


def _validate_flow_dependency_graph(steps: list[dict[str, Any]]) -> None:
    """Reject cycles among known TaskFlow step dependencies."""
    known_ids = {_text(step.get("step_id")) for step in steps}
    graph = {
        _text(step.get("step_id")): [dep for dep in tuple(step.get("depends_on") or ()) if dep in known_ids]
        for step in steps
        if _text(step.get("step_id"))
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visited:
            return
        if step_id in visiting:
            raise ValueError("task flow dependencies contain a cycle")
        visiting.add(step_id)
        for dependency_id in graph.get(step_id, []):
            visit(dependency_id)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in graph:
        visit(step_id)


def _flow_execution_projection(flow: TaskFlow | None, steps: list[TaskFlowStep]) -> dict[str, Any]:
    """Project executable DAG state from persisted TaskFlow facts."""
    by_step_id = {step.step_id: step for step in steps}
    ready_step_ids: list[str] = []
    waiting_dependency_step_ids: list[str] = []
    blocked_step_ids: list[str] = []
    active_step_ids: list[str] = []
    completed_step_ids: list[str] = []
    dependency_state: dict[str, Any] = {}
    for step in steps:
        missing_dependencies: list[str] = []
        waiting_dependencies: list[str] = []
        failed_dependencies: list[str] = []
        completed_dependencies: list[str] = []
        for dependency_id in step.depends_on:
            dependency = by_step_id.get(dependency_id)
            if dependency is None:
                missing_dependencies.append(dependency_id)
            elif dependency.status == "completed":
                completed_dependencies.append(dependency_id)
            elif dependency.status in {"failed", "cancelled"}:
                failed_dependencies.append(dependency_id)
            else:
                waiting_dependencies.append(dependency_id)
        dependency_state[step.step_id] = {
            "depends_on": list(step.depends_on),
            "completed": completed_dependencies,
            "waiting": waiting_dependencies,
            "failed": failed_dependencies,
            "missing": missing_dependencies,
            "satisfied": not (missing_dependencies or waiting_dependencies or failed_dependencies),
        }
        if step.status == "completed":
            completed_step_ids.append(step.step_id)
        if step.status in {"in_progress", "waiting_user", "waiting_approval", "blocked", "failed"}:
            active_step_ids.append(step.step_id)
        if step.status != "pending":
            continue
        if missing_dependencies or failed_dependencies:
            blocked_step_ids.append(step.step_id)
        elif waiting_dependencies:
            waiting_dependency_step_ids.append(step.step_id)
        else:
            ready_step_ids.append(step.step_id)
    all_steps_completed = bool(steps) and len(completed_step_ids) == len(steps)
    has_dependency_blocker = any(
        dependency_state[step.step_id]["missing"] or dependency_state[step.step_id]["failed"]
        for step in steps
        if step.status == "pending"
    )
    terminal_step_ids = [
        step.step_id for step in steps if step.status in {"completed", "failed", "cancelled"}
    ]
    return {
        "flow_id": flow.flow_id if flow is not None else "",
        "ready_step_ids": ready_step_ids,
        "waiting_dependency_step_ids": waiting_dependency_step_ids,
        "blocked_step_ids": blocked_step_ids,
        "active_step_ids": active_step_ids,
        "completed_step_ids": completed_step_ids,
        "terminal_step_ids": terminal_step_ids,
        "all_steps_completed": all_steps_completed,
        "has_dependency_blocker": has_dependency_blocker,
        "dependency_state": dependency_state,
    }


def _parse_items(items: Any) -> list[Any]:
    """Parse todo tool input from list or JSON string."""
    if isinstance(items, list):
        return items
    if isinstance(items, str):
        stripped = items.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return [line.strip("- ").strip() for line in stripped.splitlines()]
        return parsed if isinstance(parsed, list) else []
    return []


def _normalize_todo_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "todo": "pending",
        "open": "pending",
        "pending": "pending",
        "doing": "in_progress",
        "active": "in_progress",
        "in_progress": "in_progress",
        "in-progress": "in_progress",
        "done": "completed",
        "complete": "completed",
        "completed": "completed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }
    return aliases.get(raw, "pending")


def _normalize_flow_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "plan": "planning",
        "planning": "planning",
        "running": "running",
        "active": "running",
        "waiting": "waiting_user",
        "waiting_user": "waiting_user",
        "waiting-user": "waiting_user",
        "waiting_approval": "waiting_approval",
        "waiting-approval": "waiting_approval",
        "blocked": "blocked",
        "done": "completed",
        "complete": "completed",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }
    return aliases.get(raw, "running")


def _normalize_flow_terminal_status(value: Any) -> str:
    normalized = _normalize_flow_status(value)
    if normalized not in FLOW_TERMINAL_STATUSES:
        raise ValueError("flow terminal status must be completed, failed, or cancelled")
    return normalized


def _normalize_flow_step_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "todo": "pending",
        "open": "pending",
        "pending": "pending",
        "doing": "in_progress",
        "active": "in_progress",
        "running": "in_progress",
        "in_progress": "in_progress",
        "in-progress": "in_progress",
        "waiting": "waiting_user",
        "waiting_user": "waiting_user",
        "waiting-user": "waiting_user",
        "waiting_approval": "waiting_approval",
        "waiting-approval": "waiting_approval",
        "blocked": "blocked",
        "done": "completed",
        "complete": "completed",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }
    return aliases.get(raw, "pending")


def _normalize_sync_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"task_mirrored", "task-mirrored"}:
        return "task_mirrored"
    return "managed"


def _normalize_summary_scope(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in {"session", "goal", "flow", "task"} else "session"


def _normalize_optional_summary_scope(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in {"session", "goal", "flow", "task"} else ""


def _flow_status_after_step_update(current_status: str, step_status: str) -> str:
    if current_status in FLOW_TERMINAL_STATUSES:
        return current_status
    if step_status in {"waiting_user", "waiting_approval", "blocked"}:
        return step_status
    if step_status == "failed":
        return "blocked"
    return "running"


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return {"text": stripped}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    if value is None:
        return {}
    return {"value": value}


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _json_loads_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _compact_text(text: Any, *, max_chars: int) -> str:
    normalized = "\n".join(line.strip() for line in str(text or "").splitlines() if line.strip())
    if len(normalized) <= max_chars:
        return normalized
    head_budget = max(1, int(max_chars * 0.65))
    tail_budget = max(1, max_chars - head_budget - 40)
    return (
        normalized[:head_budget].rstrip()
        + "\n...[context summary truncated]...\n"
        + normalized[-tail_budget:].lstrip()
    )


def _truncate_text(text: str, *, max_chars: int) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(0, max_chars - 3)].rstrip() + "..."


def _rollup_summary_content(sources: list[ContextSummary], *, max_chars: int) -> str:
    """Build a compact deterministic rollup from staged summaries."""
    safe_max_chars = max(200, int(max_chars or DEFAULT_SUMMARY_MAX_CHARS))
    per_source_budget = max(120, min(700, safe_max_chars // max(1, min(len(sources), 8))))
    lines = ["Context summary rollup:"]
    for summary in reversed(sources):
        title = summary.title or summary.scope
        header = f"- [{summary.scope}/{summary.source_kind}] {title}"
        lines.append(f"{header}: {_truncate_text(summary.content, max_chars=per_source_budget)}")
    return _compact_text("\n".join(lines), max_chars=safe_max_chars)


def _connect(db_path: Path) -> sqlite3.Connection:
    path = db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_columns(conn: sqlite3.Connection, table_name: str, columns: dict[str, str]) -> None:
    """Add missing SQLite columns for lightweight schema evolution."""
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    for column_name, definition in columns.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _goal_from_row(row: sqlite3.Row) -> GoalMirror:
    return GoalMirror(
        goal_id=str(row["goal_id"]),
        session_id=str(row["session_id"]),
        status=str(row["status"]),
        objective=str(row["objective"]),
        completion_criteria=str(row["completion_criteria"]),
        current_summary=str(row["current_summary"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        completed_at_ms=int(row["completed_at_ms"]) if row["completed_at_ms"] is not None else None,
    )


def _flow_from_row(row: sqlite3.Row) -> TaskFlow:
    return TaskFlow(
        flow_id=str(row["flow_id"]),
        session_id=str(row["session_id"]),
        goal_id=str(row["goal_id"]),
        status=str(row["status"]),
        sync_mode=str(row["sync_mode"]),
        goal=str(row["goal"]),
        current_step_id=str(row["current_step_id"]),
        blocked_task_id=str(row["blocked_task_id"]),
        wait_payload=_json_loads_object(str(row["wait_json"])),
        evidence=_json_loads_object(str(row["evidence_json"])),
        revision=int(row["revision"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        completed_at_ms=int(row["completed_at_ms"]) if row["completed_at_ms"] is not None else None,
    )


def _flow_step_from_row(row: sqlite3.Row) -> TaskFlowStep:
    return TaskFlowStep(
        step_id=str(row["step_id"]),
        flow_id=str(row["flow_id"]),
        session_id=str(row["session_id"]),
        order_index=int(row["order_index"]),
        title=str(row["title"]),
        status=str(row["status"]),
        task_id=str(row["task_id"]),
        depends_on=tuple(_text(item) for item in _json_loads_list(str(row["depends_on_json"])) if _text(item)),
        evidence=_json_loads_object(str(row["evidence_json"])),
        last_error=str(row["last_error"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        completed_at_ms=int(row["completed_at_ms"]) if row["completed_at_ms"] is not None else None,
    )


def _summary_from_row(row: sqlite3.Row) -> ContextSummary:
    return ContextSummary(
        summary_id=str(row["summary_id"]),
        session_id=str(row["session_id"]),
        scope=str(row["scope"]),
        goal_id=str(row["goal_id"]),
        flow_id=str(row["flow_id"]),
        task_id=str(row["task_id"]),
        title=str(row["title"]),
        content=str(row["content"]),
        source_kind=str(row["source_kind"]),
        metadata=_json_loads_object(str(row["metadata_json"])),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _todo_from_row(row: sqlite3.Row) -> TodoItem:
    return TodoItem(
        todo_id=str(row["todo_id"]),
        goal_id=str(row["goal_id"]),
        session_id=str(row["session_id"]),
        order_index=int(row["order_index"]),
        content=str(row["content"]),
        status=str(row["status"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _json_loads_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _text(value: Any) -> str:
    return str(value or "").strip()
