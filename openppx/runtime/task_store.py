"""SQLite-backed long-task fact storage for openppx."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import get_data_dir


TASK_ACTIVE_STATUSES = frozenset(
    {
        "queued",
        "running",
        "paused",
        "waiting_user",
        "waiting_approval",
        "interrupted",
        "stale",
    }
)
TASK_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "lost"})


@dataclass(frozen=True, slots=True)
class TaskRun:
    """A durable, user-visible unit of supervised execution."""

    task_id: str
    kind: str
    status: str
    title: str
    owner_key: str
    user_id: str
    thread_id: str
    session_id: str
    turn_id: str
    invocation_id: str
    function_call_id: str
    tool_call_id: str
    dedupe_key: str
    external_ref: str
    runner_payload_json: str
    runner_capabilities_json: str
    resume_policy: str
    stop_policy: str
    cancel_policy: str
    checkpoint_ref: str
    lease_owner: str
    lease_expires_at_ms: int | None
    claim_token: str
    progress_summary: str
    terminal_summary: str
    last_error: str
    version: int
    created_at_ms: int
    updated_at_ms: int
    ended_at_ms: int | None

    @property
    def runner_payload(self) -> dict[str, Any]:
        """Return the decoded runner payload."""
        return _json_loads(self.runner_payload_json)

    @property
    def runner_capabilities(self) -> dict[str, Any]:
        """Return the decoded runner capability declaration."""
        return _json_loads(self.runner_capabilities_json)


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """Append-only event for one task."""

    event_id: int
    task_id: str
    event_type: str
    message: str
    payload_json: str
    created_at_ms: int

    @property
    def payload(self) -> dict[str, Any]:
        """Return the decoded event payload."""
        return _json_loads(self.payload_json)


@dataclass(frozen=True, slots=True)
class TaskInput:
    """Durable user-provided input for one waiting task."""

    input_id: int
    task_id: str
    content: str
    payload_json: str
    consumed_at_ms: int | None
    created_at_ms: int

    @property
    def payload(self) -> dict[str, Any]:
        """Return the decoded input payload."""
        return _json_loads(self.payload_json)


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """Idempotency record for one effectful tool invocation."""

    idempotency_key: str
    tool_name: str
    args_hash: str
    status: str
    task_id: str
    result_json: str
    error: str
    created_at_ms: int
    updated_at_ms: int

    @property
    def result(self) -> dict[str, Any]:
        """Return the decoded stored result."""
        return _json_loads(self.result_json)


@dataclass(frozen=True, slots=True)
class TaskDelivery:
    """Once-only delivery record for a task notification side effect."""

    delivery_key: str
    task_id: str
    delivery_type: str
    payload_json: str
    status: str
    attempts: int
    last_error: str
    next_attempt_at_ms: int | None
    delivered_at_ms: int | None
    ack_status: str
    ack_payload_json: str
    provider_message_id: str
    acked_at_ms: int | None
    created_at_ms: int

    @property
    def payload(self) -> dict[str, Any]:
        """Return the decoded delivery payload."""
        return _json_loads(self.payload_json)

    @property
    def ack_payload(self) -> dict[str, Any]:
        """Return the decoded acknowledgement payload."""
        return _json_loads(self.ack_payload_json)


@dataclass(frozen=True, slots=True)
class TaskArtifact:
    """Durable artifact index entry for one task."""

    artifact_id: int
    task_id: str
    artifact_type: str
    label: str
    media_type: str
    path: str
    size_bytes: int
    metadata_json: str
    created_at_ms: int

    @property
    def metadata(self) -> dict[str, Any]:
        """Return the decoded artifact metadata."""
        return _json_loads(self.metadata_json)


@dataclass(frozen=True, slots=True)
class TaskCheckpoint:
    """Durable runner checkpoint fact for one task."""

    checkpoint_id: str
    task_id: str
    checkpoint_type: str
    runner_name: str
    payload_json: str
    summary: str
    created_at_ms: int

    @property
    def payload(self) -> dict[str, Any]:
        """Return the decoded checkpoint payload."""
        return _json_loads(self.payload_json)


def _now_ms() -> int:
    """Return current wall-clock time in milliseconds."""
    return int(time.time() * 1000)


def _json_dumps(payload: Any) -> str:
    """Serialize a JSON value for SQLite storage."""
    return json.dumps(payload if payload is not None else {}, ensure_ascii=False, default=str)


def _json_loads(raw: str | None) -> dict[str, Any]:
    """Deserialize one JSON object, returning an empty object on invalid input."""
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def task_db_path() -> Path:
    """Return the default SQLite path for long-task runtime facts."""
    explicit = os.getenv("OPENPPX_TASK_DB_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return get_data_dir() / "database" / "tasks.db"


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection using the runtime's small-write defaults."""
    path = db_path or task_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_columns(conn: sqlite3.Connection, table_name: str, columns: dict[str, str]) -> None:
    """Add missing SQLite columns using additive, backward-compatible DDL."""
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Return whether one SQLite table exists."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _checkpoint_retention_scope(
    *,
    task_id: str | None = None,
    session_id: str | None = None,
) -> tuple[str, list[Any]]:
    """Build the checkpoint retention SQL scope clause."""
    conditions: list[str] = []
    params: list[Any] = []
    if task_id:
        conditions.append("task.task_id = ?")
        params.append(str(task_id).strip())
    if session_id:
        conditions.append("task.session_id = ?")
        params.append(str(session_id).strip())
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, params


def _task_from_row(row: sqlite3.Row) -> TaskRun:
    """Project one task row into a dataclass."""
    return TaskRun(
        task_id=str(row["task_id"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        title=str(row["title"]),
        owner_key=str(row["owner_key"]),
        user_id=str(row["user_id"]),
        thread_id=str(row["thread_id"]),
        session_id=str(row["session_id"]),
        turn_id=str(row["turn_id"]),
        invocation_id=str(row["invocation_id"]),
        function_call_id=str(row["function_call_id"]),
        tool_call_id=str(row["tool_call_id"]),
        dedupe_key=str(row["dedupe_key"]),
        external_ref=str(row["external_ref"]),
        runner_payload_json=str(row["runner_payload_json"]),
        runner_capabilities_json=str(row["runner_capabilities_json"]),
        resume_policy=str(row["resume_policy"]),
        stop_policy=str(row["stop_policy"]),
        cancel_policy=str(row["cancel_policy"]),
        checkpoint_ref=str(row["checkpoint_ref"]),
        lease_owner=str(row["lease_owner"]),
        lease_expires_at_ms=row["lease_expires_at_ms"],
        claim_token=str(row["claim_token"]),
        progress_summary=str(row["progress_summary"]),
        terminal_summary=str(row["terminal_summary"]),
        last_error=str(row["last_error"]),
        version=int(row["version"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        ended_at_ms=row["ended_at_ms"],
    )


def _event_from_row(row: sqlite3.Row) -> TaskEvent:
    """Project one event row into a dataclass."""
    return TaskEvent(
        event_id=int(row["event_id"]),
        task_id=str(row["task_id"]),
        event_type=str(row["event_type"]),
        message=str(row["message"]),
        payload_json=str(row["payload_json"]),
        created_at_ms=int(row["created_at_ms"]),
    )


def _input_from_row(row: sqlite3.Row) -> TaskInput:
    """Project one task input row into a dataclass."""
    return TaskInput(
        input_id=int(row["input_id"]),
        task_id=str(row["task_id"]),
        content=str(row["content"]),
        payload_json=str(row["payload_json"]),
        consumed_at_ms=row["consumed_at_ms"],
        created_at_ms=int(row["created_at_ms"]),
    )


def _tool_record_from_row(row: sqlite3.Row) -> ToolCallRecord:
    """Project one tool-call record row into a dataclass."""
    return ToolCallRecord(
        idempotency_key=str(row["idempotency_key"]),
        tool_name=str(row["tool_name"]),
        args_hash=str(row["args_hash"]),
        status=str(row["status"]),
        task_id=str(row["task_id"]),
        result_json=str(row["result_json"]),
        error=str(row["error"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _delivery_from_row(row: sqlite3.Row) -> TaskDelivery:
    """Project one delivery row into a dataclass."""
    return TaskDelivery(
        delivery_key=str(row["delivery_key"]),
        task_id=str(row["task_id"]),
        delivery_type=str(row["delivery_type"]),
        payload_json=str(row["payload_json"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        last_error=str(row["last_error"]),
        next_attempt_at_ms=row["next_attempt_at_ms"],
        delivered_at_ms=row["delivered_at_ms"],
        ack_status=str(row["ack_status"]),
        ack_payload_json=str(row["ack_payload_json"]),
        provider_message_id=str(row["provider_message_id"]),
        acked_at_ms=row["acked_at_ms"],
        created_at_ms=int(row["created_at_ms"]),
    )


def _artifact_from_row(row: sqlite3.Row) -> TaskArtifact:
    """Project one artifact row into a dataclass."""
    return TaskArtifact(
        artifact_id=int(row["artifact_id"]),
        task_id=str(row["task_id"]),
        artifact_type=str(row["artifact_type"]),
        label=str(row["label"]),
        media_type=str(row["media_type"]),
        path=str(row["path"]),
        size_bytes=int(row["size_bytes"]),
        metadata_json=str(row["metadata_json"]),
        created_at_ms=int(row["created_at_ms"]),
    )


def _checkpoint_from_row(row: sqlite3.Row) -> TaskCheckpoint:
    """Project one checkpoint row into a dataclass."""
    return TaskCheckpoint(
        checkpoint_id=str(row["checkpoint_id"]),
        task_id=str(row["task_id"]),
        checkpoint_type=str(row["checkpoint_type"]),
        runner_name=str(row["runner_name"]),
        payload_json=str(row["payload_json"]),
        summary=str(row["summary"]),
        created_at_ms=int(row["created_at_ms"]),
    )


class TaskStore:
    """Store and update minimal `TaskRun` facts."""

    _UPDATE_FIELDS = frozenset(
        {
            "status",
            "title",
            "external_ref",
            "runner_payload_json",
            "runner_capabilities_json",
            "resume_policy",
            "stop_policy",
            "cancel_policy",
            "checkpoint_ref",
            "lease_owner",
            "lease_expires_at_ms",
            "claim_token",
            "progress_summary",
            "terminal_summary",
            "last_error",
            "ended_at_ms",
        }
    )

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path is not None else task_db_path()
        self._lock = threading.Lock()
        self.ensure_schema()

    @property
    def db_path(self) -> Path:
        """Return the backing SQLite path."""
        return self._db_path

    def ensure_schema(self) -> None:
        """Create task tables and indexes when missing."""
        with _connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_runs (
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
                    runner_capabilities_json TEXT NOT NULL DEFAULT '{}',
                    resume_policy TEXT NOT NULL DEFAULT '',
                    stop_policy TEXT NOT NULL DEFAULT '',
                    cancel_policy TEXT NOT NULL DEFAULT '',
                    checkpoint_ref TEXT NOT NULL DEFAULT '',
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
            _ensure_columns(
                conn,
                "task_runs",
                {
                    "runner_capabilities_json": "TEXT NOT NULL DEFAULT '{}'",
                    "resume_policy": "TEXT NOT NULL DEFAULT ''",
                    "stop_policy": "TEXT NOT NULL DEFAULT ''",
                    "cancel_policy": "TEXT NOT NULL DEFAULT ''",
                    "checkpoint_ref": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_session ON task_runs(session_id, updated_at_ms DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_thread ON task_runs(thread_id, updated_at_ms DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_status ON task_runs(status, updated_at_ms DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_dedupe ON task_runs(dedupe_key, status)")

    def create_task(
        self,
        *,
        kind: str,
        title: str,
        status: str = "queued",
        owner_key: str = "",
        user_id: str = "",
        thread_id: str = "",
        session_id: str = "",
        turn_id: str = "",
        invocation_id: str = "",
        function_call_id: str = "",
        tool_call_id: str = "",
        dedupe_key: str = "",
        external_ref: str = "",
        runner_payload: dict[str, Any] | None = None,
        runner_capabilities: dict[str, Any] | None = None,
        resume_policy: str = "",
        stop_policy: str = "",
        cancel_policy: str = "",
        checkpoint_ref: str = "",
        progress_summary: str = "",
        terminal_summary: str = "",
        last_error: str = "",
        task_id: str | None = None,
    ) -> TaskRun:
        """Create one task and return the stored row."""
        now_ms = _now_ms()
        resolved_task_id = task_id or f"task_{uuid.uuid4().hex[:16]}"
        with self._lock, _connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, kind, status, title, owner_key, user_id, thread_id,
                    session_id, turn_id, invocation_id, function_call_id, tool_call_id,
                    dedupe_key, external_ref, runner_payload_json,
                    runner_capabilities_json, resume_policy, stop_policy,
                    cancel_policy, checkpoint_ref, lease_owner, lease_expires_at_ms,
                    claim_token, progress_summary, terminal_summary, last_error,
                    version, created_at_ms, updated_at_ms, ended_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_task_id,
                    kind,
                    status,
                    title,
                    owner_key,
                    user_id,
                    thread_id,
                    session_id,
                    turn_id,
                    invocation_id,
                    function_call_id,
                    tool_call_id,
                    dedupe_key,
                    external_ref,
                    _json_dumps(runner_payload or {}),
                    _json_dumps(runner_capabilities or {}),
                    resume_policy,
                    stop_policy,
                    cancel_policy,
                    checkpoint_ref,
                    "",
                    None,
                    "",
                    progress_summary,
                    terminal_summary,
                    last_error,
                    1,
                    now_ms,
                    now_ms,
                    now_ms if status in TASK_TERMINAL_STATUSES else None,
                ),
            )
        task = self.get_task(resolved_task_id)
        assert task is not None
        return task

    def get_task(self, task_id: str) -> TaskRun | None:
        """Return one task by id."""
        with _connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()
        return _task_from_row(row) if row is not None else None

    def get_task_by_dedupe_key(
        self,
        dedupe_key: str,
        *,
        statuses: list[str] | tuple[str, ...] | None = None,
    ) -> TaskRun | None:
        """Return the most recent task for one idempotency key."""
        normalized = str(dedupe_key or "").strip()
        if not normalized:
            return None
        conditions = ["dedupe_key = ?"]
        params: list[Any] = [normalized]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)
        where = " AND ".join(conditions)
        with _connect(self._db_path) as conn:
            row = conn.execute(
                f"SELECT * FROM task_runs WHERE {where} ORDER BY updated_at_ms DESC LIMIT 1",
                tuple(params),
            ).fetchone()
        return _task_from_row(row) if row is not None else None

    def list_tasks(
        self,
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        statuses: list[str] | tuple[str, ...] | None = None,
        limit: int = 20,
    ) -> list[TaskRun]:
        """List tasks ordered by most recent update."""
        conditions: list[str] = []
        params: list[Any] = []
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if thread_id:
            conditions.append("thread_id = ?")
            params.append(thread_id)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        safe_limit = max(1, min(int(limit or 20), 200))
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM task_runs {where} ORDER BY updated_at_ms DESC LIMIT ?",
                (*params, safe_limit),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def list_claimable_tasks(
        self,
        *,
        statuses: list[str] | tuple[str, ...] | None = None,
        limit: int = 20,
        now_ms: int | None = None,
    ) -> list[TaskRun]:
        """List active tasks whose lease is absent or expired."""
        selected_statuses = list(statuses or TASK_ACTIVE_STATUSES)
        placeholders = ", ".join("?" for _ in selected_statuses)
        current_ms = _now_ms() if now_ms is None else int(now_ms)
        safe_limit = max(1, min(int(limit or 20), 500))
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM task_runs
                WHERE status IN ({placeholders})
                  AND (lease_expires_at_ms IS NULL OR lease_expires_at_ms <= ?)
                ORDER BY updated_at_ms ASC
                LIMIT ?
                """,
                (*selected_statuses, current_ms, safe_limit),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def count_by_status(self, *, session_id: str | None = None) -> dict[str, int]:
        """Return task counts grouped by status."""
        conditions: list[str] = []
        params: list[Any] = []
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                f"SELECT status, COUNT(*) AS count FROM task_runs {where} GROUP BY status ORDER BY status",
                params,
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def list_stuck_tasks(
        self,
        *,
        older_than_ms: int,
        statuses: list[str] | tuple[str, ...] | None = None,
        session_id: str | None = None,
        now_ms: int | None = None,
        limit: int = 50,
    ) -> list[TaskRun]:
        """List non-terminal tasks that have not changed within the age budget."""
        selected_statuses = list(statuses or TASK_ACTIVE_STATUSES)
        placeholders = ", ".join("?" for _ in selected_statuses)
        cutoff_ms = (_now_ms() if now_ms is None else int(now_ms)) - max(0, int(older_than_ms))
        conditions = [f"status IN ({placeholders})", "updated_at_ms <= ?"]
        params: list[Any] = [*selected_statuses, cutoff_ms]
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        safe_limit = max(1, min(int(limit or 50), 500))
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM task_runs
                WHERE {' AND '.join(conditions)}
                ORDER BY updated_at_ms ASC
                LIMIT ?
                """,
                (*params, safe_limit),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def list_terminal_tasks_older_than(
        self,
        *,
        older_than_ms: int,
        session_id: str | None = None,
        now_ms: int | None = None,
        limit: int = 100,
    ) -> list[TaskRun]:
        """List terminal tasks eligible for retention cleanup."""
        cutoff_ms = (_now_ms() if now_ms is None else int(now_ms)) - max(0, int(older_than_ms))
        conditions = [f"status IN ({', '.join('?' for _ in TASK_TERMINAL_STATUSES)})", "updated_at_ms <= ?"]
        params: list[Any] = [*sorted(TASK_TERMINAL_STATUSES), cutoff_ms]
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        safe_limit = max(1, min(int(limit or 100), 1000))
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM task_runs
                WHERE {' AND '.join(conditions)}
                ORDER BY updated_at_ms ASC
                LIMIT ?
                """,
                (*params, safe_limit),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def delete_tasks(self, task_ids: list[str] | tuple[str, ...]) -> int:
        """Delete task facts and TaskRuntime child records for explicit task ids.

        ToolCallRecord rows are intentionally retained because they are the
        execution-level idempotency ledger for external side effects.
        """
        normalized_ids = [str(task_id or "").strip() for task_id in task_ids if str(task_id or "").strip()]
        if not normalized_ids:
            return 0
        placeholders = ", ".join("?" for _ in normalized_ids)
        with self._lock, _connect(self._db_path) as conn:
            for table in (
                "task_events",
                "task_inputs",
                "task_deliveries",
                "task_artifacts",
                "task_checkpoints",
            ):
                if _table_exists(conn, table):
                    conn.execute(f"DELETE FROM {table} WHERE task_id IN ({placeholders})", normalized_ids)
            cursor = conn.execute(f"DELETE FROM task_runs WHERE task_id IN ({placeholders})", normalized_ids)
        return int(cursor.rowcount or 0)

    def claim_task(
        self,
        task_id: str,
        *,
        lease_owner: str,
        lease_ms: int,
        now_ms: int | None = None,
    ) -> TaskRun | None:
        """Claim one task if its lease is currently available."""
        current_ms = _now_ms() if now_ms is None else int(now_ms)
        token = uuid.uuid4().hex
        expires_at = current_ms + max(1, int(lease_ms))
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE task_runs
                SET lease_owner = ?,
                    lease_expires_at_ms = ?,
                    claim_token = ?,
                    version = version + 1
                WHERE task_id = ?
                  AND status NOT IN ('completed', 'failed', 'cancelled', 'lost')
                  AND (lease_expires_at_ms IS NULL OR lease_expires_at_ms <= ?)
                """,
                (lease_owner, expires_at, token, task_id, current_ms),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_task(task_id)

    def release_claim(self, task_id: str, *, lease_owner: str, claim_token: str) -> bool:
        """Release a task lease when owner and token still match."""
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE task_runs
                SET lease_owner = '',
                    lease_expires_at_ms = NULL,
                    claim_token = '',
                    version = version + 1
                WHERE task_id = ?
                  AND lease_owner = ?
                  AND claim_token = ?
                """,
                (task_id, lease_owner, claim_token),
            )
        return cursor.rowcount > 0

    def update_task(
        self,
        task_id: str,
        *,
        expected_version: int | None = None,
        runner_payload: dict[str, Any] | None = None,
        runner_capabilities: dict[str, Any] | None = None,
        **fields: Any,
    ) -> TaskRun | None:
        """Update selected task fields, optionally using optimistic locking."""
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in self._UPDATE_FIELDS:
                raise ValueError(f"unsupported task update field {key!r}")
            updates[key] = value
        if runner_payload is not None:
            updates["runner_payload_json"] = _json_dumps(runner_payload)
        if runner_capabilities is not None:
            updates["runner_capabilities_json"] = _json_dumps(runner_capabilities)
        if not updates:
            return self.get_task(task_id)
        status = updates.get("status")
        if status in TASK_TERMINAL_STATUSES and "ended_at_ms" not in updates:
            updates["ended_at_ms"] = _now_ms()
        updates["updated_at_ms"] = _now_ms()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        params = list(updates.values())
        params.append(task_id)
        version_clause = ""
        if expected_version is not None:
            version_clause = " AND version = ?"
            params.append(expected_version)
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                f"UPDATE task_runs SET {assignments}, version = version + 1 WHERE task_id = ?{version_clause}",
                params,
            )
            if cursor.rowcount == 0:
                return None
        return self.get_task(task_id)


class TaskEventStore:
    """Store append-only task events."""

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path is not None else task_db_path()
        self._lock = threading.Lock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create task event tables and indexes when missing."""
        with _connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, event_id)")

    def append_event(
        self,
        task_id: str,
        event_type: str,
        *,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> TaskEvent:
        """Append and return one event."""
        now_ms = _now_ms()
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO task_events (task_id, event_type, message, payload_json, created_at_ms)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, event_type, message, _json_dumps(payload or {}), now_ms),
            )
            event_id = int(cursor.lastrowid)
            row = conn.execute("SELECT * FROM task_events WHERE event_id = ?", (event_id,)).fetchone()
        assert row is not None
        return _event_from_row(row)

    def list_events(self, task_id: str, *, limit: int = 50) -> list[TaskEvent]:
        """List recent events for a task in chronological order."""
        safe_limit = max(1, min(int(limit or 50), 500))
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (task_id, safe_limit),
            ).fetchall()
        return [_event_from_row(row) for row in rows]


class TaskInputStore:
    """Store user inputs supplied to waiting tasks."""

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path is not None else task_db_path()
        self._lock = threading.Lock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create task input tables and indexes when missing."""
        with _connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_inputs (
                    input_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    consumed_at_ms INTEGER,
                    created_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_inputs_task ON task_inputs(task_id, input_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_inputs_unconsumed ON task_inputs(task_id, consumed_at_ms)"
            )

    def append_input(
        self,
        task_id: str,
        content: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> TaskInput:
        """Append user input for one task and return the stored input."""
        now_ms = _now_ms()
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO task_inputs (task_id, content, payload_json, consumed_at_ms, created_at_ms)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, content, _json_dumps(payload or {}), None, now_ms),
            )
            input_id = int(cursor.lastrowid)
            row = conn.execute("SELECT * FROM task_inputs WHERE input_id = ?", (input_id,)).fetchone()
        assert row is not None
        return _input_from_row(row)

    def list_inputs(self, task_id: str, *, include_consumed: bool = True, limit: int = 50) -> list[TaskInput]:
        """List recent inputs for one task in chronological order."""
        safe_limit = max(1, min(int(limit or 50), 500))
        consumed_clause = "" if include_consumed else "AND consumed_at_ms IS NULL"
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM task_inputs
                WHERE task_id = ?
                {consumed_clause}
                ORDER BY input_id ASC
                LIMIT ?
                """,
                (task_id, safe_limit),
            ).fetchall()
        return [_input_from_row(row) for row in rows]

    def mark_consumed(self, input_id: int, *, consumed_at_ms: int | None = None) -> TaskInput | None:
        """Mark one input consumed by a runner."""
        current_ms = _now_ms() if consumed_at_ms is None else int(consumed_at_ms)
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE task_inputs
                SET consumed_at_ms = ?
                WHERE input_id = ?
                """,
                (current_ms, input_id),
            )
            if cursor.rowcount == 0:
                return None
        with _connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM task_inputs WHERE input_id = ?", (input_id,)).fetchone()
        return _input_from_row(row) if row is not None else None


class ToolCallRecordStore:
    """Store tool-call idempotency records."""

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path is not None else task_db_path()
        self._lock = threading.Lock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create tool-call record tables when missing."""
        with _connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_call_records (
                    idempotency_key TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    args_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_call_records_task ON tool_call_records(task_id)")

    def create_or_get(
        self,
        *,
        idempotency_key: str,
        tool_name: str,
        args_hash: str,
    ) -> tuple[ToolCallRecord, bool]:
        """Create a pending record, or return the existing one."""
        now_ms = _now_ms()
        with self._lock, _connect(self._db_path) as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO tool_call_records (
                        idempotency_key, tool_name, args_hash, status, task_id,
                        result_json, error, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (idempotency_key, tool_name, args_hash, "pending", "", "{}", "", now_ms, now_ms),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = conn.execute(
                "SELECT * FROM tool_call_records WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        assert row is not None
        return _tool_record_from_row(row), created

    def link_task(self, idempotency_key: str, task_id: str, *, status: str = "running") -> ToolCallRecord | None:
        """Attach a materialized task to one tool-call record."""
        return self.update_record(idempotency_key, task_id=task_id, status=status)

    def settle(
        self,
        idempotency_key: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> ToolCallRecord | None:
        """Persist the final or replayable result for a tool call."""
        return self.update_record(
            idempotency_key,
            status=status,
            result_json=_json_dumps(result or {}),
            error=error,
        )

    def update_record(self, idempotency_key: str, **fields: Any) -> ToolCallRecord | None:
        """Update selected idempotency record fields."""
        allowed = {"status", "task_id", "result_json", "error"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_record(idempotency_key)
        updates["updated_at_ms"] = _now_ms()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                f"UPDATE tool_call_records SET {assignments} WHERE idempotency_key = ?",
                (*updates.values(), idempotency_key),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_record(idempotency_key)

    def get_record(self, idempotency_key: str) -> ToolCallRecord | None:
        """Return one tool-call record."""
        with _connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM tool_call_records WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return _tool_record_from_row(row) if row is not None else None


class TaskDeliveryStore:
    """Store once-only delivery records for task status notifications."""

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path is not None else task_db_path()
        self._lock = threading.Lock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create delivery tables when missing."""
        with _connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_deliveries (
                    delivery_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    delivery_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'delivered',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    next_attempt_at_ms INTEGER,
                    delivered_at_ms INTEGER,
                    ack_status TEXT NOT NULL DEFAULT 'none',
                    ack_payload_json TEXT NOT NULL DEFAULT '{}',
                    provider_message_id TEXT NOT NULL DEFAULT '',
                    acked_at_ms INTEGER,
                    created_at_ms INTEGER NOT NULL
                )
                """
            )
            _ensure_columns(
                conn,
                "task_deliveries",
                {
                    "status": "TEXT NOT NULL DEFAULT 'delivered'",
                    "attempts": "INTEGER NOT NULL DEFAULT 0",
                    "last_error": "TEXT NOT NULL DEFAULT ''",
                    "next_attempt_at_ms": "INTEGER",
                    "delivered_at_ms": "INTEGER",
                    "ack_status": "TEXT NOT NULL DEFAULT 'none'",
                    "ack_payload_json": "TEXT NOT NULL DEFAULT '{}'",
                    "provider_message_id": "TEXT NOT NULL DEFAULT ''",
                    "acked_at_ms": "INTEGER",
                },
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_deliveries_task ON task_deliveries(task_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_deliveries_retry "
                "ON task_deliveries(status, next_attempt_at_ms)"
            )

    def record_once(
        self,
        *,
        task_id: str,
        delivery_type: str,
        payload: dict[str, Any] | None = None,
        delivery_key: str | None = None,
    ) -> tuple[TaskDelivery, bool]:
        """Create a pending delivery record if it does not already exist."""
        key = delivery_key or f"{task_id}:{delivery_type}"
        now_ms = _now_ms()
        with self._lock, _connect(self._db_path) as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO task_deliveries (
                        delivery_key, task_id, delivery_type, payload_json,
                        status, attempts, last_error, next_attempt_at_ms,
                        delivered_at_ms, ack_status, ack_payload_json,
                        provider_message_id, acked_at_ms, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        task_id,
                        delivery_type,
                        _json_dumps(payload or {}),
                        "pending",
                        0,
                        "",
                        now_ms,
                        None,
                        "none",
                        "{}",
                        "",
                        None,
                        now_ms,
                    ),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = conn.execute(
                "SELECT * FROM task_deliveries WHERE delivery_key = ?",
                (key,),
            ).fetchone()
        assert row is not None
        return _delivery_from_row(row), created

    def mark_delivered(
        self,
        delivery_key: str,
        *,
        delivered_at_ms: int | None = None,
        ack_payload: dict[str, Any] | None = None,
        provider_message_id: str = "",
    ) -> TaskDelivery | None:
        """Mark one delivery as successfully published."""
        now_ms = _now_ms() if delivered_at_ms is None else int(delivered_at_ms)
        normalized_ack = ack_payload or {}
        normalized_provider_message_id = str(
            provider_message_id or normalized_ack.get("provider_message_id") or normalized_ack.get("message_id") or ""
        )
        ack_status = "provider_receipt" if normalized_ack or normalized_provider_message_id else "published"
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE task_deliveries
                SET status = 'delivered',
                    attempts = attempts + 1,
                    last_error = '',
                    next_attempt_at_ms = NULL,
                    delivered_at_ms = ?,
                    ack_status = ?,
                    ack_payload_json = ?,
                    provider_message_id = ?,
                    acked_at_ms = ?
                WHERE delivery_key = ?
                """,
                (
                    now_ms,
                    ack_status,
                    _json_dumps(normalized_ack),
                    normalized_provider_message_id,
                    now_ms,
                    delivery_key,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM task_deliveries WHERE delivery_key = ?", (delivery_key,)).fetchone()
        return _delivery_from_row(row) if row is not None else None

    def mark_failed(
        self,
        delivery_key: str,
        *,
        error: str,
        retry_after_ms: int,
        failed_at_ms: int | None = None,
    ) -> TaskDelivery | None:
        """Mark one delivery attempt failed and schedule a retry."""
        now_ms = _now_ms() if failed_at_ms is None else int(failed_at_ms)
        next_attempt_at_ms = now_ms + max(0, int(retry_after_ms))
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE task_deliveries
                SET status = 'failed',
                    attempts = attempts + 1,
                    last_error = ?,
                    next_attempt_at_ms = ?,
                    delivered_at_ms = NULL
                WHERE delivery_key = ?
                """,
                (str(error or "")[:2000], next_attempt_at_ms, delivery_key),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM task_deliveries WHERE delivery_key = ?", (delivery_key,)).fetchone()
        return _delivery_from_row(row) if row is not None else None

    def list_retryable_deliveries(self, *, now_ms: int | None = None, limit: int = 50) -> list[TaskDelivery]:
        """Return pending or failed deliveries due for publication."""
        current_ms = _now_ms() if now_ms is None else int(now_ms)
        safe_limit = max(1, min(int(limit or 50), 500))
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_deliveries
                WHERE status IN ('pending', 'failed')
                  AND (next_attempt_at_ms IS NULL OR next_attempt_at_ms <= ?)
                ORDER BY created_at_ms ASC
                LIMIT ?
                """,
                (current_ms, safe_limit),
            ).fetchall()
        return [_delivery_from_row(row) for row in rows]

    def list_deliveries(self, task_id: str) -> list[TaskDelivery]:
        """Return delivery records for one task."""
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_deliveries
                WHERE task_id = ?
                ORDER BY created_at_ms ASC
                """,
                (task_id,),
            ).fetchall()
        return [_delivery_from_row(row) for row in rows]

    def summarize_by_task_ids(self, task_ids: list[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
        """Return lightweight delivery summaries keyed by task id."""
        normalized_ids = [str(task_id) for task_id in task_ids if str(task_id or "").strip()]
        if not normalized_ids:
            return {}
        placeholders = ", ".join("?" for _ in normalized_ids)
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM task_deliveries
                WHERE task_id IN ({placeholders})
                ORDER BY task_id ASC, created_at_ms ASC
                """,
                tuple(normalized_ids),
            ).fetchall()
        summaries: dict[str, dict[str, Any]] = {}
        for delivery in (_delivery_from_row(row) for row in rows):
            summary = summaries.setdefault(
                delivery.task_id,
                {
                    "count": 0,
                    "pending_count": 0,
                    "failed_count": 0,
                    "delivered_count": 0,
                    "latest": None,
                },
            )
            summary["count"] += 1
            status_key = f"{delivery.status}_count"
            if status_key in summary:
                summary[status_key] += 1
            summary["latest"] = {
                "delivery_key": delivery.delivery_key,
                "delivery_type": delivery.delivery_type,
                "status": delivery.status,
                "attempts": delivery.attempts,
                "last_error": delivery.last_error,
                "next_attempt_at_ms": delivery.next_attempt_at_ms,
                "delivered_at_ms": delivery.delivered_at_ms,
                "ack_status": delivery.ack_status,
                "ack_payload": delivery.ack_payload,
                "provider_message_id": delivery.provider_message_id,
                "acked_at_ms": delivery.acked_at_ms,
                "created_at_ms": delivery.created_at_ms,
            }
        return summaries


class TaskArtifactStore:
    """Store durable task artifact index entries."""

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path is not None else task_db_path()
        self._lock = threading.Lock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create task artifact tables when missing."""
        with _connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_artifacts (
                    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_artifacts_task ON task_artifacts(task_id, artifact_id)"
            )

    def record_artifact(
        self,
        *,
        task_id: str,
        artifact_type: str,
        label: str,
        media_type: str,
        path: str,
        size_bytes: int,
        metadata: dict[str, Any] | None = None,
    ) -> TaskArtifact:
        """Record one task artifact and return the stored row."""
        now_ms = _now_ms()
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO task_artifacts (
                    task_id, artifact_type, label, media_type, path,
                    size_bytes, metadata_json, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    artifact_type,
                    label,
                    media_type,
                    path,
                    max(0, int(size_bytes)),
                    _json_dumps(metadata or {}),
                    now_ms,
                ),
            )
            artifact_id = int(cursor.lastrowid)
            row = conn.execute("SELECT * FROM task_artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        assert row is not None
        return _artifact_from_row(row)

    def list_artifacts(self, task_id: str, *, limit: int = 50) -> list[TaskArtifact]:
        """List task artifacts in creation order."""
        safe_limit = max(1, min(int(limit or 50), 500))
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_artifacts
                WHERE task_id = ?
                ORDER BY artifact_id ASC
                LIMIT ?
                """,
                (task_id, safe_limit),
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def count_orphaned_artifacts(self) -> int:
        """Return task artifact index rows whose parent task no longer exists."""
        with _connect(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM task_artifacts AS artifact
                LEFT JOIN task_runs AS task ON task.task_id = artifact.task_id
                WHERE task.task_id IS NULL
                """
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def list_orphaned_artifacts(self, *, limit: int = 100) -> list[TaskArtifact]:
        """List task artifact index rows whose parent task no longer exists."""
        safe_limit = max(1, min(int(limit or 100), 1000))
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT artifact.*
                FROM task_artifacts AS artifact
                LEFT JOIN task_runs AS task ON task.task_id = artifact.task_id
                WHERE task.task_id IS NULL
                ORDER BY artifact.artifact_id ASC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def delete_artifact_records(self, artifact_ids: list[int] | tuple[int, ...]) -> int:
        """Delete explicit task artifact index rows without touching files."""
        normalized_ids = [int(artifact_id) for artifact_id in artifact_ids if int(artifact_id) > 0]
        if not normalized_ids:
            return 0
        placeholders = ", ".join("?" for _ in normalized_ids)
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                f"DELETE FROM task_artifacts WHERE artifact_id IN ({placeholders})",
                normalized_ids,
            )
        return int(cursor.rowcount or 0)


class TaskCheckpointStore:
    """Store durable task checkpoint facts."""

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path is not None else task_db_path()
        self._lock = threading.Lock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create task checkpoint tables when missing."""
        with _connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    checkpoint_type TEXT NOT NULL,
                    runner_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_checkpoints_task ON task_checkpoints(task_id, created_at_ms DESC)"
            )

    def record_checkpoint(
        self,
        *,
        task_id: str,
        checkpoint_type: str = "runner",
        runner_name: str = "",
        payload: dict[str, Any] | None = None,
        summary: str = "",
        checkpoint_id: str | None = None,
    ) -> TaskCheckpoint:
        """Record one checkpoint fact and return the stored row."""
        now_ms = _now_ms()
        resolved_checkpoint_id = checkpoint_id or f"ckpt_{uuid.uuid4().hex[:16]}"
        with self._lock, _connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO task_checkpoints (
                    checkpoint_id, task_id, checkpoint_type, runner_name,
                    payload_json, summary, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_checkpoint_id,
                    task_id,
                    str(checkpoint_type or "").strip() or "runner",
                    str(runner_name or "").strip(),
                    _json_dumps(payload or {}),
                    str(summary or "").strip(),
                    now_ms,
                ),
            )
            row = conn.execute(
                "SELECT * FROM task_checkpoints WHERE checkpoint_id = ?",
                (resolved_checkpoint_id,),
            ).fetchone()
        assert row is not None
        return _checkpoint_from_row(row)

    def get_checkpoint(self, checkpoint_id: str) -> TaskCheckpoint | None:
        """Return one checkpoint by id."""
        with _connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM task_checkpoints WHERE checkpoint_id = ?",
                (str(checkpoint_id or "").strip(),),
            ).fetchone()
        return _checkpoint_from_row(row) if row is not None else None

    def list_checkpoints(self, task_id: str, *, limit: int = 20) -> list[TaskCheckpoint]:
        """List checkpoints for one task, newest first."""
        safe_limit = max(1, min(int(limit or 20), 200))
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_checkpoints
                WHERE task_id = ?
                ORDER BY created_at_ms DESC
                LIMIT ?
                """,
                (task_id, safe_limit),
            ).fetchall()
        return [_checkpoint_from_row(row) for row in rows]

    def count_orphaned_checkpoints(self) -> int:
        """Return checkpoint rows whose parent task no longer exists."""
        with _connect(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM task_checkpoints AS checkpoint
                LEFT JOIN task_runs AS task ON task.task_id = checkpoint.task_id
                WHERE task.task_id IS NULL
                """
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def list_orphaned_checkpoints(self, *, limit: int = 100) -> list[TaskCheckpoint]:
        """List checkpoint rows whose parent task no longer exists."""
        safe_limit = max(1, min(int(limit or 100), 1000))
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT checkpoint.*
                FROM task_checkpoints AS checkpoint
                LEFT JOIN task_runs AS task ON task.task_id = checkpoint.task_id
                WHERE task.task_id IS NULL
                ORDER BY checkpoint.created_at_ms ASC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [_checkpoint_from_row(row) for row in rows]

    def count_retention_candidates(
        self,
        *,
        older_than_ms: int,
        keep_latest_per_task: int = 3,
        task_id: str | None = None,
        session_id: str | None = None,
        now_ms: int | None = None,
    ) -> int:
        """Return checkpoint rows eligible for retention cleanup."""
        cutoff_ms = (_now_ms() if now_ms is None else int(now_ms)) - max(0, int(older_than_ms))
        keep_latest = max(0, min(int(keep_latest_per_task or 0), 100))
        where, params = _checkpoint_retention_scope(task_id=task_id, session_id=session_id)
        with _connect(self._db_path) as conn:
            row = conn.execute(
                f"""
                WITH ranked AS (
                    SELECT
                        checkpoint.checkpoint_id,
                        checkpoint.created_at_ms,
                        task.checkpoint_ref AS current_checkpoint_ref,
                        ROW_NUMBER() OVER (
                            PARTITION BY checkpoint.task_id
                            ORDER BY checkpoint.created_at_ms DESC, checkpoint.checkpoint_id DESC
                        ) AS checkpoint_rank
                    FROM task_checkpoints AS checkpoint
                    INNER JOIN task_runs AS task ON task.task_id = checkpoint.task_id
                    {where}
                )
                SELECT COUNT(*) AS count
                FROM ranked
                WHERE created_at_ms <= ?
                  AND checkpoint_id != current_checkpoint_ref
                  AND checkpoint_rank > ?
                """,
                (*params, cutoff_ms, keep_latest),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def list_retention_candidates(
        self,
        *,
        older_than_ms: int,
        keep_latest_per_task: int = 3,
        task_id: str | None = None,
        session_id: str | None = None,
        now_ms: int | None = None,
        limit: int = 100,
    ) -> list[TaskCheckpoint]:
        """List old non-current checkpoints that can be cleaned up.

        The current `TaskRun.checkpoint_ref` is never returned, and the newest
        `keep_latest_per_task` checkpoints for each task are kept even if old.
        """
        cutoff_ms = (_now_ms() if now_ms is None else int(now_ms)) - max(0, int(older_than_ms))
        keep_latest = max(0, min(int(keep_latest_per_task or 0), 100))
        safe_limit = max(1, min(int(limit or 100), 1000))
        where, params = _checkpoint_retention_scope(task_id=task_id, session_id=session_id)
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                f"""
                WITH ranked AS (
                    SELECT
                        checkpoint.*,
                        task.checkpoint_ref AS current_checkpoint_ref,
                        ROW_NUMBER() OVER (
                            PARTITION BY checkpoint.task_id
                            ORDER BY checkpoint.created_at_ms DESC, checkpoint.checkpoint_id DESC
                        ) AS checkpoint_rank
                    FROM task_checkpoints AS checkpoint
                    INNER JOIN task_runs AS task ON task.task_id = checkpoint.task_id
                    {where}
                )
                SELECT *
                FROM ranked
                WHERE created_at_ms <= ?
                  AND checkpoint_id != current_checkpoint_ref
                  AND checkpoint_rank > ?
                ORDER BY created_at_ms ASC, checkpoint_id ASC
                LIMIT ?
                """,
                (*params, cutoff_ms, keep_latest, safe_limit),
            ).fetchall()
        return [_checkpoint_from_row(row) for row in rows]

    def delete_checkpoints(self, checkpoint_ids: list[str] | tuple[str, ...]) -> int:
        """Delete explicit checkpoint rows."""
        normalized_ids = [
            str(checkpoint_id or "").strip()
            for checkpoint_id in checkpoint_ids
            if str(checkpoint_id or "").strip()
        ]
        if not normalized_ids:
            return 0
        placeholders = ", ".join("?" for _ in normalized_ids)
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                f"DELETE FROM task_checkpoints WHERE checkpoint_id IN ({placeholders})",
                normalized_ids,
            )
        return int(cursor.rowcount or 0)

    def delete_retention_checkpoints(self, checkpoint_ids: list[str] | tuple[str, ...]) -> int:
        """Delete checkpoint rows while preserving any current task checkpoint refs."""
        normalized_ids = [
            str(checkpoint_id or "").strip()
            for checkpoint_id in checkpoint_ids
            if str(checkpoint_id or "").strip()
        ]
        if not normalized_ids:
            return 0
        placeholders = ", ".join("?" for _ in normalized_ids)
        with self._lock, _connect(self._db_path) as conn:
            cursor = conn.execute(
                f"""
                DELETE FROM task_checkpoints
                WHERE checkpoint_id IN ({placeholders})
                  AND NOT EXISTS (
                    SELECT 1
                    FROM task_runs AS task
                    WHERE task.checkpoint_ref = task_checkpoints.checkpoint_id
                  )
                """,
                normalized_ids,
            )
        return int(cursor.rowcount or 0)
