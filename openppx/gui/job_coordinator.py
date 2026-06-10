"""Durable current-process GUI job coordination.

This module gives GUI/browser-style workflows an explicit job boundary:
submit, status, output, pause/cancel, and resume from a stored checkpoint.
It does not pretend a Python thread can survive process restart. Running jobs
that are no longer attached to this process are reported as stale.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..core.config import get_data_dir
from ..runtime.sync_tool_proxy import SyncCancellationToken, SyncProxyCancelled
from .checkpoint import normalize_gui_task_checkpoint
from .task_runner import execute_gui_task


DEFAULT_GUI_JOB_MAX_WORKERS = 4
_JOBS: dict[str, concurrent.futures.Future[Any]] = {}
_TOKENS: dict[str, SyncCancellationToken] = {}
_LOCK = threading.Lock()
_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class GuiJob:
    """Durable GUI job fact."""

    job_id: str
    status: str
    task: str
    request_json: str
    checkpoint_json: str
    result_json: str
    error: str
    desired_stop_status: str
    created_at_ms: int
    updated_at_ms: int
    ended_at_ms: int | None

    @property
    def request(self) -> dict[str, Any]:
        """Return decoded request payload."""
        return _json_loads(self.request_json)

    @property
    def checkpoint(self) -> dict[str, Any]:
        """Return decoded checkpoint payload."""
        return _json_loads(self.checkpoint_json)

    @property
    def result(self) -> dict[str, Any]:
        """Return decoded result payload."""
        return _json_loads(self.result_json)


class GuiJobStore:
    """SQLite-backed GUI job state store."""

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path is not None else gui_job_db_path()
        self._lock = threading.Lock()
        self.ensure_schema()

    @property
    def db_path(self) -> Path:
        """Return underlying SQLite path."""
        return self._db_path

    def ensure_schema(self) -> None:
        """Create GUI job schema when missing."""
        with _connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gui_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    task TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    desired_stop_status TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER
                )
                """
            )

    def create_job(
        self,
        *,
        job_id: str,
        task: str,
        request: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
    ) -> GuiJob:
        """Create one GUI job row."""
        now_ms = _now_ms()
        with self._lock, _connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO gui_jobs (
                    job_id, status, task, request_json, checkpoint_json,
                    result_json, error, desired_stop_status,
                    created_at_ms, updated_at_ms, ended_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "running",
                    task,
                    _json_dumps(request),
                    _json_dumps(checkpoint or {}),
                    _json_dumps({}),
                    "",
                    "",
                    now_ms,
                    now_ms,
                    None,
                ),
            )
        job = self.get_job(job_id)
        assert job is not None
        return job

    def get_job(self, job_id: str) -> GuiJob | None:
        """Return one GUI job by id."""
        with _connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM gui_jobs WHERE job_id = ?",
                (str(job_id or "").strip(),),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def update_job(self, job_id: str, **fields: Any) -> GuiJob | None:
        """Update selected GUI job fields."""
        allowed = {
            "status",
            "checkpoint",
            "result",
            "error",
            "desired_stop_status",
            "ended_at_ms",
        }
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unsupported GUI job update field {key!r}")
            if key == "checkpoint":
                updates["checkpoint_json"] = _json_dumps(value or {})
            elif key == "result":
                updates["result_json"] = _json_dumps(value or {})
            else:
                updates[key] = value
        if not updates:
            return self.get_job(job_id)
        status = str(updates.get("status") or "")
        if status in {"completed", "failed", "cancelled", "interrupted", "paused", "lost"}:
            updates.setdefault("ended_at_ms", _now_ms())
        updates["updated_at_ms"] = _now_ms()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        params = list(updates.values())
        params.append(str(job_id or "").strip())
        with self._lock, _connect(self._db_path) as conn:
            conn.execute(f"UPDATE gui_jobs SET {assignments} WHERE job_id = ?", params)
        return self.get_job(job_id)


def submit_gui_task_job(
    *,
    task: str,
    max_steps: int | None = None,
    dry_run: bool = False,
    planner_model: str | None = None,
    planner_api_key: str | None = None,
    planner_base_url: str | None = None,
    initial_state: dict[str, Any] | None = None,
    parent_job_id: str = "",
    store: GuiJobStore | None = None,
    executor: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Submit one GUI task as a current-process durable job."""
    normalized = str(task or "").strip()
    if not normalized:
        return {"ok": False, "error": "task is required"}
    job_store = store or GuiJobStore()
    job_id = f"gui_job_{uuid.uuid4().hex[:16]}"
    request = {
        "task": normalized,
        "max_steps": max_steps,
        "dry_run": bool(dry_run),
        "planner_model": planner_model,
        "planner_api_key_present": bool(planner_api_key),
        "planner_base_url": planner_base_url or "",
        "parent_job_id": parent_job_id,
    }
    try:
        checkpoint = _initial_checkpoint(
            task=normalized,
            max_steps=max_steps,
            dry_run=dry_run,
            initial_state=initial_state,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    job_store.create_job(job_id=job_id, task=normalized, request=request, checkpoint=checkpoint)
    token = SyncCancellationToken()
    run_callable = executor or execute_gui_task
    future = _executor().submit(
        _run_gui_job,
        job_id,
        job_store,
        run_callable,
        normalized,
        max_steps,
        bool(dry_run),
        planner_model,
        planner_api_key,
        planner_base_url,
        token,
        checkpoint,
    )
    with _LOCK:
        _JOBS[job_id] = future
        _TOKENS[job_id] = token
    future.add_done_callback(lambda _future: _unregister_job(job_id))
    return {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "task": normalized,
        "checkpoint": checkpoint,
    }


def gui_task_job_status(job_id: str, *, store: GuiJobStore | None = None) -> dict[str, Any]:
    """Return one GUI job status snapshot."""
    job_store = store or GuiJobStore()
    job = _refresh_attachment_status(job_store, job_id)
    if job is None:
        return {"ok": False, "error": f"GUI job {job_id!r} not found"}
    return _job_payload(job)


def gui_task_job_output(job_id: str, *, store: GuiJobStore | None = None) -> dict[str, Any]:
    """Return one GUI job output payload."""
    status = gui_task_job_status(job_id, store=store)
    if not status.get("ok"):
        return status
    result = status.get("result") if isinstance(status.get("result"), dict) else {}
    checkpoint = status.get("checkpoint") if isinstance(status.get("checkpoint"), dict) else {}
    return {
        "ok": True,
        "job_id": status["job_id"],
        "status": status["status"],
        "output": result or checkpoint,
        "summary": status.get("summary", ""),
        "checkpoint": checkpoint,
    }


def gui_task_job_cancel(
    job_id: str,
    *,
    terminal_status: str = "cancelled",
    reason: str = "",
    store: GuiJobStore | None = None,
) -> dict[str, Any]:
    """Request cooperative stop for one GUI job."""
    normalized = str(terminal_status or "").strip().lower()
    if normalized not in {"cancelled", "interrupted", "paused"}:
        normalized = "cancelled"
    job_store = store or GuiJobStore()
    job = job_store.get_job(job_id)
    if job is None:
        return {"ok": False, "error": f"GUI job {job_id!r} not found"}
    if job.status in {"completed", "failed", "cancelled", "interrupted", "paused", "lost"}:
        return {"ok": True, "job_id": job.job_id, "status": job.status, "action": "already_terminal"}
    with _LOCK:
        token = _TOKENS.get(job.job_id)
    if token is None:
        updated = job_store.update_job(
            job.job_id,
            status="stale",
            error="GUI job is not attached to this process.",
        )
        return _job_payload(updated or job) | {"action": "detached"}
    job_store.update_job(job.job_id, desired_stop_status=normalized)
    token.request_stop(
        terminal_status="interrupted" if normalized == "paused" else normalized,
        reason=reason or f"GUI job {normalized} by request.",
    )
    return {"ok": True, "job_id": job.job_id, "status": "stop_requested", "action": normalized}


def resume_gui_task_job(
    *,
    checkpoint: dict[str, Any],
    store: GuiJobStore | None = None,
    executor: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resume a GUI task from a checkpoint by submitting a new job."""
    try:
        resume_checkpoint = normalize_gui_task_checkpoint(checkpoint, include_schema=True)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    task = str(resume_checkpoint.get("task", "") or "").strip()
    if not task:
        return {"ok": False, "error": "checkpoint task is required"}
    return submit_gui_task_job(
        task=task,
        max_steps=_maybe_int(resume_checkpoint.get("max_steps")),
        dry_run=bool(resume_checkpoint.get("dry_run", False)),
        initial_state=resume_checkpoint,
        parent_job_id=str(resume_checkpoint.get("job_id", "") or ""),
        store=store,
        executor=executor,
    )


def _run_gui_job(
    job_id: str,
    store: GuiJobStore,
    run_callable: Callable[..., dict[str, Any]],
    task: str,
    max_steps: int | None,
    dry_run: bool,
    planner_model: str | None,
    planner_api_key: str | None,
    planner_base_url: str | None,
    token: SyncCancellationToken,
    initial_state: dict[str, Any],
) -> None:
    """Run and settle a GUI job."""
    def checkpoint_callback(state: dict[str, Any]) -> None:
        payload = normalize_gui_task_checkpoint(
            {
                **state,
                "job_id": job_id,
            },
            task=task,
            max_steps=max_steps,
            dry_run=dry_run,
            include_schema=True,
        )
        store.update_job(job_id, checkpoint=payload)

    try:
        result = run_callable(
            task=task,
            max_steps=max_steps,
            dry_run=dry_run,
            planner_model=planner_model,
            planner_api_key=planner_api_key,
            planner_base_url=planner_base_url,
            cancel_token=token,
            initial_state=initial_state,
            checkpoint_callback=checkpoint_callback,
        )
    except SyncProxyCancelled as exc:
        job = store.get_job(job_id)
        desired = (job.desired_stop_status if job is not None else "") or exc.terminal_status
        status = "paused" if desired == "paused" else exc.terminal_status
        checkpoint = job.checkpoint if job is not None else {}
        result = {
            "ok": False,
            "finished": False,
            "status_code": status,
            "error": exc.reason,
            "final_summary": exc.reason,
            "checkpoint": checkpoint,
        }
        store.update_job(job_id, status=status, result=result, error=exc.reason)
        return
    except Exception as exc:
        store.update_job(job_id, status="failed", result={"ok": False, "error": str(exc)}, error=str(exc))
        return

    status = _status_from_result(result)
    error = str(result.get("error", "") or "") if isinstance(result, dict) else ""
    store.update_job(job_id, status=status, result=result, error=error)


def _status_from_result(result: dict[str, Any]) -> str:
    """Map GUI task result to job status."""
    status_code = str(result.get("status_code", "") or "").strip().lower()
    if bool(result.get("ok")) and bool(result.get("finished")):
        return "completed"
    if status_code in {"cancelled", "interrupted", "paused", "completed", "failed"}:
        return status_code
    return "failed"


def _job_payload(job: GuiJob) -> dict[str, Any]:
    """Project one GUI job to a public payload."""
    result = job.result
    checkpoint = job.checkpoint
    summary = (
        str(result.get("final_summary") or result.get("message") or "")
        if isinstance(result, dict)
        else ""
    )
    if not summary:
        summary = str(checkpoint.get("summary") or checkpoint.get("current_plan") or job.error or "")
    return {
        "ok": True,
        "job_id": job.job_id,
        "status": job.status,
        "task": job.task,
        "summary": summary,
        "error": job.error,
        "request": job.request,
        "checkpoint": checkpoint,
        "result": result,
        "created_at_ms": job.created_at_ms,
        "updated_at_ms": job.updated_at_ms,
        "ended_at_ms": job.ended_at_ms,
        "attached": is_gui_job_attached(job.job_id),
    }


def is_gui_job_attached(job_id: str) -> bool:
    """Return whether one GUI job future is attached in this process."""
    with _LOCK:
        future = _JOBS.get(str(job_id))
    return bool(future is not None and not future.done())


def _refresh_attachment_status(store: GuiJobStore, job_id: str) -> GuiJob | None:
    """Mark unattached running jobs as stale."""
    job = store.get_job(job_id)
    if job is None:
        return None
    if job.status == "running" and not is_gui_job_attached(job.job_id):
        return store.update_job(job.job_id, status="stale", error="GUI job is not attached to this process.") or job
    return job


def _initial_checkpoint(
    *,
    task: str,
    max_steps: int | None,
    dry_run: bool,
    initial_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build an initial checkpoint payload."""
    return normalize_gui_task_checkpoint(
        initial_state,
        task=task,
        max_steps=max_steps,
        dry_run=dry_run,
        current_plan=task,
        include_schema=True,
    )


def _unregister_job(job_id: str) -> None:
    """Remove attached future/token after completion."""
    with _LOCK:
        _JOBS.pop(str(job_id), None)
        _TOKENS.pop(str(job_id), None)


def _executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return bounded GUI job executor."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers())
        return _EXECUTOR


def _max_workers() -> int:
    raw = os.getenv("OPENPPX_GUI_JOB_MAX_WORKERS", "").strip()
    try:
        parsed = int(raw) if raw else DEFAULT_GUI_JOB_MAX_WORKERS
    except ValueError:
        parsed = DEFAULT_GUI_JOB_MAX_WORKERS
    return max(1, min(parsed, 32))


def gui_job_db_path() -> Path:
    """Return GUI job database path."""
    configured = os.getenv("OPENPPX_GUI_JOB_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return get_data_dir() / "gui_jobs.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open one GUI job SQLite connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _job_from_row(row: sqlite3.Row) -> GuiJob:
    """Project one SQLite row."""
    return GuiJob(
        job_id=str(row["job_id"]),
        status=str(row["status"]),
        task=str(row["task"]),
        request_json=str(row["request_json"]),
        checkpoint_json=str(row["checkpoint_json"]),
        result_json=str(row["result_json"]),
        error=str(row["error"]),
        desired_stop_status=str(row["desired_stop_status"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        ended_at_ms=row["ended_at_ms"] if row["ended_at_ms"] is None else int(row["ended_at_ms"]),
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _maybe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None
