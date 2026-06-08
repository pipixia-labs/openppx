"""Runtime-length proxy for synchronous builtin tool calls."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .task_store import TaskEventStore, TaskStore, ToolCallRecordStore


DEFAULT_SYNC_PROXY_INLINE_BUDGET_MS = 5_000
MAX_SYNC_PROXY_INLINE_BUDGET_MS = 120_000
MAX_SYNC_PROXY_SUMMARY_CHARS = 4_000
SYNC_PROXY_RUNNER_CAPABILITIES: dict[str, bool] = {
    "status": True,
    "cancel": False,
    "interrupt": False,
    "output": True,
    "artifact": False,
    "rejoin": True,
    "pause": False,
    "checkpoint": False,
}

_BACKGROUND_FUTURES: dict[str, concurrent.futures.Future[Any]] = {}
_BACKGROUND_CANCEL_TOKENS: dict[str, "SyncCancellationToken"] = {}
_BACKGROUND_LOCK = threading.Lock()
_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


class SyncProxyCancelled(Exception):
    """Raised by cooperative sync tools after a stop request is observed."""

    def __init__(self, *, terminal_status: str = "interrupted", reason: str = "") -> None:
        normalized = str(terminal_status or "").strip().lower()
        if normalized not in {"interrupted", "cancelled"}:
            normalized = "interrupted"
        self.terminal_status = normalized
        self.reason = str(reason or "").strip() or f"Sync proxy task {normalized} by user request."
        super().__init__(self.reason)


class SyncCancellationToken:
    """Cooperative stop token for sync proxy call implementations.

    The token does not kill Python threads. Tool code must call
    :meth:`check_cancelled` at explainable boundaries and stop itself.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._terminal_status = "interrupted"
        self._reason = ""

    @property
    def is_cancel_requested(self) -> bool:
        """Return whether a stop request has been made."""
        return self._event.is_set()

    @property
    def terminal_status(self) -> str:
        """Return the requested terminal status."""
        with self._lock:
            return self._terminal_status

    @property
    def reason(self) -> str:
        """Return the requested stop reason."""
        with self._lock:
            return self._reason

    def request_stop(self, *, terminal_status: str = "interrupted", reason: str = "") -> None:
        """Request cooperative stop at the next tool-defined boundary."""
        normalized = str(terminal_status or "").strip().lower()
        if normalized not in {"interrupted", "cancelled"}:
            normalized = "interrupted"
        with self._lock:
            self._terminal_status = normalized
            self._reason = str(reason or "").strip() or f"Sync proxy task {normalized} by user request."
            self._event.set()

    def check_cancelled(self) -> None:
        """Raise when a cooperative stop request has been made."""
        if self._event.is_set():
            raise SyncProxyCancelled(terminal_status=self.terminal_status, reason=self.reason)


@dataclass(frozen=True, slots=True)
class SyncProxyInvocationContext:
    """Metadata that binds one sync proxy call to an ADK invocation."""

    user_id: str = ""
    session_id: str = ""
    thread_id: str = ""
    turn_id: str = ""
    invocation_id: str = ""
    function_call_id: str = ""
    tool_call_id: str = ""
    owner_key: str = ""


def run_sync_callable_with_proxy(
    *,
    tool_name: str,
    title: str,
    kind: str,
    call: Callable[[], Any],
    args_for_hash: Any,
    tool_context: Any | None = None,
    inline_budget_ms: int | None = None,
    runner_payload: dict[str, Any] | None = None,
    cancel_token: SyncCancellationToken | None = None,
    task_store: TaskStore | None = None,
    event_store: TaskEventStore | None = None,
    tool_call_store: ToolCallRecordStore | None = None,
) -> Any:
    """Run a synchronous callable inline or expose it as a TaskRun.

    The callable is submitted immediately to a bounded thread pool. If it
    finishes inside the inline budget, the original result is returned. If it is
    still running after the budget, the function returns a task payload and the
    thread continues in the current process.
    """
    normalized_tool_name = str(tool_name or "").strip() or "sync_tool"
    resolved_budget_ms = normalize_sync_proxy_inline_budget_ms(inline_budget_ms)
    stores = _SyncProxyStores.from_optional(
        task_store=task_store,
        event_store=event_store,
        tool_call_store=tool_call_store,
    )
    context = context_from_tool_context(tool_context)
    args_hash = _stable_hash({"tool": normalized_tool_name, "args": args_for_hash})
    idempotency_key = _idempotency_key(context=context, tool_name=normalized_tool_name, args_hash=args_hash)
    record, created = stores.tool_call_store.create_or_get(
        idempotency_key=idempotency_key,
        tool_name=normalized_tool_name,
        args_hash=args_hash,
    )
    if not created:
        replayed = _replay_sync_proxy_record(record, stores=stores)
        if replayed is not None:
            return replayed

    future = _executor().submit(call)
    try:
        result = future.result(timeout=resolved_budget_ms / 1000.0)
    except concurrent.futures.TimeoutError:
        task = _materialize_sync_proxy_task(
            stores=stores,
            context=context,
            tool_name=normalized_tool_name,
            title=title,
            kind=kind,
            args_hash=args_hash,
            idempotency_key=idempotency_key,
            inline_budget_ms=resolved_budget_ms,
            runner_payload=runner_payload or {},
            cancel_token=cancel_token,
        )
        _register_background_future(task.task_id, future, cancel_token=cancel_token)
        future.add_done_callback(
            lambda completed: _finalize_sync_proxy_task(
                task_id=task.task_id,
                future=completed,
                stores=stores,
                idempotency_key=idempotency_key,
            )
        )
        return _task_started_payload(task, replayed=False)
    except Exception as exc:
        stores.tool_call_store.settle(idempotency_key, status="failed", error=str(exc))
        raise

    status, error = _status_for_sync_result(result)
    stores.tool_call_store.settle(
        idempotency_key,
        status=status,
        result={"tool_result": result},
        error=error,
    )
    return result


def is_sync_proxy_task_attached(task_id: str) -> bool:
    """Return whether a sync proxy background future is still attached here."""
    with _BACKGROUND_LOCK:
        return str(task_id) in _BACKGROUND_FUTURES


def request_sync_proxy_task_stop(
    task_id: str,
    *,
    terminal_status: str = "interrupted",
    reason: str = "",
) -> bool:
    """Request cooperative stop for an attached sync proxy task."""
    with _BACKGROUND_LOCK:
        token = _BACKGROUND_CANCEL_TOKENS.get(str(task_id))
    if token is None:
        return False
    token.request_stop(terminal_status=terminal_status, reason=reason)
    return True


def normalize_sync_proxy_inline_budget_ms(value: int | None) -> int:
    """Clamp sync proxy inline wait budget to safe bounds."""
    if value is None:
        return _default_inline_budget_ms()
    try:
        parsed = int(value)
    except Exception:
        return _default_inline_budget_ms()
    return max(0, min(parsed, MAX_SYNC_PROXY_INLINE_BUDGET_MS))


def context_from_tool_context(tool_context: Any | None) -> SyncProxyInvocationContext:
    """Extract stable invocation metadata from ADK ToolContext-like objects."""
    if tool_context is None:
        return SyncProxyInvocationContext()
    session = getattr(tool_context, "session", None)
    invocation_id = _text(getattr(tool_context, "invocation_id", ""))
    if not invocation_id:
        invocation_context = getattr(tool_context, "_invocation_context", None)
        invocation_id = _text(getattr(invocation_context, "invocation_id", ""))
    function_call_id = _text(getattr(tool_context, "function_call_id", ""))
    session_id = _text(getattr(session, "id", ""))
    user_id = _text(getattr(tool_context, "user_id", "")) or _text(getattr(session, "user_id", ""))
    return SyncProxyInvocationContext(
        user_id=user_id,
        session_id=session_id,
        thread_id=session_id,
        turn_id=invocation_id,
        invocation_id=invocation_id,
        function_call_id=function_call_id,
        tool_call_id=function_call_id,
        owner_key=user_id,
    )


@dataclass(frozen=True, slots=True)
class _SyncProxyStores:
    task_store: TaskStore
    event_store: TaskEventStore
    tool_call_store: ToolCallRecordStore

    @classmethod
    def from_optional(
        cls,
        *,
        task_store: TaskStore | None,
        event_store: TaskEventStore | None,
        tool_call_store: ToolCallRecordStore | None,
    ) -> "_SyncProxyStores":
        resolved_task_store = task_store or TaskStore()
        return cls(
            task_store=resolved_task_store,
            event_store=event_store or TaskEventStore(db_path=resolved_task_store.db_path),
            tool_call_store=tool_call_store or ToolCallRecordStore(db_path=resolved_task_store.db_path),
        )


def _materialize_sync_proxy_task(
    *,
    stores: _SyncProxyStores,
    context: SyncProxyInvocationContext,
    tool_name: str,
    title: str,
    kind: str,
    args_hash: str,
    idempotency_key: str,
    inline_budget_ms: int,
    runner_payload: dict[str, Any],
    cancel_token: SyncCancellationToken | None,
) -> Any:
    """Create TaskRun facts for one sync call that exceeded inline budget."""
    progress = f"Tool `{tool_name}` is still running after {inline_budget_ms} ms."
    cooperative_cancel = cancel_token is not None
    payload = {
        "runner": "sync_tool_proxy",
        "tool_name": tool_name,
        "inline_budget_ms": inline_budget_ms,
        "started_at_ms": _wall_now_ms(),
        "cooperative_cancel": cooperative_cancel,
        "status_snapshot": {
            "status": "running",
            "message": progress,
        },
    }
    payload.update(runner_payload)
    capabilities = dict(SYNC_PROXY_RUNNER_CAPABILITIES)
    if cooperative_cancel:
        capabilities.update({"cancel": True, "interrupt": True})
    task = stores.task_store.create_task(
        kind=str(kind or "").strip() or "sync_tool",
        status="running",
        title=str(title or "").strip() or tool_name,
        owner_key=context.owner_key or context.user_id,
        user_id=context.user_id,
        thread_id=context.thread_id or context.session_id,
        session_id=context.session_id,
        turn_id=context.turn_id or context.invocation_id,
        invocation_id=context.invocation_id,
        function_call_id=context.function_call_id,
        tool_call_id=context.tool_call_id,
        dedupe_key=f"sync_tool:{tool_name}:{args_hash}",
        external_ref="",
        runner_payload=payload,
        runner_capabilities=capabilities,
        resume_policy="rejoin",
        stop_policy="cooperative_cancel" if cooperative_cancel else "not_stoppable",
        cancel_policy="cooperative_cancel" if cooperative_cancel else "not_cancellable",
        progress_summary=progress,
    )
    stores.event_store.append_event(
        task.task_id,
        "task.started",
        message=progress,
        payload={"runner": "sync_tool_proxy", "tool_name": tool_name, "inline_budget_ms": inline_budget_ms},
    )
    stores.tool_call_store.link_task(idempotency_key, task.task_id, status="running")
    return task


def _finalize_sync_proxy_task(
    *,
    task_id: str,
    future: concurrent.futures.Future[Any],
    stores: _SyncProxyStores,
    idempotency_key: str,
) -> None:
    """Update TaskRun facts when a background sync future finishes."""
    try:
        result = future.result()
    except SyncProxyCancelled as exc:
        _settle_cancelled_background_task(
            task_id=task_id,
            stores=stores,
            idempotency_key=idempotency_key,
            exc=exc,
        )
    except Exception as exc:
        _settle_failed_background_task(task_id=task_id, stores=stores, idempotency_key=idempotency_key, exc=exc)
    else:
        _settle_completed_background_task(
            task_id=task_id,
            stores=stores,
            idempotency_key=idempotency_key,
            result=result,
        )
    finally:
        _unregister_background_future(task_id, future)


def _settle_cancelled_background_task(
    *,
    task_id: str,
    stores: _SyncProxyStores,
    idempotency_key: str,
    exc: SyncProxyCancelled,
) -> None:
    """Persist cooperative sync stop acknowledged by the running tool."""
    current = stores.task_store.get_task(task_id)
    status = exc.terminal_status
    summary = exc.reason
    if current is None:
        stores.tool_call_store.settle(idempotency_key, status=status, error=summary)
        return
    if current.status != "running":
        stores.tool_call_store.settle(
            idempotency_key,
            status=current.status,
            result={"mode": "task", "status": current.status, "task_id": task_id},
            error=current.last_error,
        )
        return
    updated = stores.task_store.update_task(
        task_id,
        status=status,
        terminal_summary=summary,
        progress_summary=summary,
        last_error="" if status == "interrupted" else summary,
        resume_policy="not_resumable",
    )
    stores.tool_call_store.settle(
        idempotency_key,
        status=status,
        result={"mode": "task", "status": status, "task_id": task_id},
        error="" if status == "interrupted" else summary,
    )
    if updated is not None:
        stores.event_store.append_event(
            task_id,
            "task.cancelled" if status == "cancelled" else "task.interrupted",
            message=summary,
            payload={"runner": "sync_tool_proxy", "cooperative": True},
        )


def _settle_completed_background_task(
    *,
    task_id: str,
    stores: _SyncProxyStores,
    idempotency_key: str,
    result: Any,
) -> None:
    """Persist background sync completion or tool-level failure."""
    current = stores.task_store.get_task(task_id)
    if current is None:
        return
    if current.status != "running":
        stores.tool_call_store.settle(
            idempotency_key,
            status=current.status,
            result={"mode": "task", "status": current.status, "task_id": task_id},
            error=current.last_error,
        )
        return
    status, error = _status_for_sync_result(result)
    summary = _summarize_sync_result(result)
    updated = stores.task_store.update_task(
        task_id,
        status=status,
        terminal_summary=summary,
        progress_summary=summary,
        last_error=error,
        resume_policy="not_resumable",
    )
    stores.tool_call_store.settle(
        idempotency_key,
        status=status,
        result={"mode": "task", "status": status, "task_id": task_id, "tool_result": result},
        error=error,
    )
    if updated is not None:
        stores.event_store.append_event(
            task_id,
            "task.completed" if status == "completed" else "task.failed",
            message=summary,
            payload={"runner": "sync_tool_proxy", "error": error},
        )


def _settle_failed_background_task(
    *,
    task_id: str,
    stores: _SyncProxyStores,
    idempotency_key: str,
    exc: Exception,
) -> None:
    """Persist an exception raised by a background sync future."""
    current = stores.task_store.get_task(task_id)
    error = str(exc) or type(exc).__name__
    if current is None:
        stores.tool_call_store.settle(idempotency_key, status="failed", error=error)
        return
    if current.status != "running":
        stores.tool_call_store.settle(
            idempotency_key,
            status=current.status,
            result={"mode": "task", "status": current.status, "task_id": task_id},
            error=current.last_error or error,
        )
        return
    updated = stores.task_store.update_task(
        task_id,
        status="failed",
        terminal_summary=error,
        progress_summary=error,
        last_error=error,
        resume_policy="not_resumable",
    )
    stores.tool_call_store.settle(idempotency_key, status="failed", error=error)
    if updated is not None:
        stores.event_store.append_event(
            task_id,
            "task.failed",
            message=error,
            payload={"runner": "sync_tool_proxy", "error_type": type(exc).__name__},
        )


def _replay_sync_proxy_record(record: Any, *, stores: _SyncProxyStores) -> Any | None:
    """Return a replay payload for duplicate sync proxy invocations when safe."""
    if record.task_id:
        task = stores.task_store.get_task(record.task_id)
        if task is not None:
            return _task_started_payload(task, replayed=True)
    if record.status == "completed":
        result = record.result
        if "tool_result" in result:
            return result["tool_result"]
    return None


def _task_started_payload(task: Any, *, replayed: bool) -> dict[str, Any]:
    """Return the tool result for a materialized sync proxy task."""
    payload: dict[str, Any] = {
        "ok": True,
        "mode": "task",
        "status": task.status,
        "task_id": task.task_id,
        "title": task.title,
        "progress_summary": task.progress_summary,
    }
    if replayed:
        payload["replayed"] = True
    return payload


def _status_for_sync_result(result: Any) -> tuple[str, str]:
    """Classify a sync tool result as completed or failed."""
    if isinstance(result, dict):
        if result.get("ok") is False:
            return "failed", _text(result.get("error") or result.get("message") or "tool returned ok=false")
        error = result.get("error")
        if isinstance(error, str) and error.strip():
            return "failed", error.strip()
    return "completed", ""


def _summarize_sync_result(result: Any) -> str:
    """Return a compact JSON/text summary for one sync result."""
    if isinstance(result, dict):
        for key in ("message", "error", "final_summary"):
            value = _text(result.get(key))
            if value:
                return value[:MAX_SYNC_PROXY_SUMMARY_CHARS]
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        text = str(result)
    text = text.strip() or "(no tool result)"
    if len(text) <= MAX_SYNC_PROXY_SUMMARY_CHARS:
        return text
    keep = max(500, MAX_SYNC_PROXY_SUMMARY_CHARS - 80)
    return f"Tool result exceeded summary budget ({len(text)} chars).\n\nTail:\n{text[-keep:]}"


def _register_background_future(
    task_id: str,
    future: concurrent.futures.Future[Any],
    *,
    cancel_token: SyncCancellationToken | None = None,
) -> None:
    """Attach one background future to a TaskRun id."""
    with _BACKGROUND_LOCK:
        _BACKGROUND_FUTURES[task_id] = future
        if cancel_token is not None:
            _BACKGROUND_CANCEL_TOKENS[task_id] = cancel_token


def _unregister_background_future(task_id: str, future: concurrent.futures.Future[Any]) -> None:
    """Detach a background future if it is still the current handle."""
    with _BACKGROUND_LOCK:
        if _BACKGROUND_FUTURES.get(task_id) is future:
            _BACKGROUND_FUTURES.pop(task_id, None)
            _BACKGROUND_CANCEL_TOKENS.pop(task_id, None)


def _executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return the global sync proxy thread pool."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=_max_workers(),
                thread_name_prefix="openppx-sync-tool-proxy",
            )
        return _EXECUTOR


def _max_workers() -> int:
    raw = os.getenv("OPENPPX_SYNC_PROXY_MAX_WORKERS", "").strip()
    try:
        value = int(raw) if raw else 4
    except ValueError:
        value = 4
    return max(1, min(value, 32))


def _default_inline_budget_ms() -> int:
    raw = os.getenv("OPENPPX_SYNC_PROXY_INLINE_BUDGET_MS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_SYNC_PROXY_INLINE_BUDGET_MS
    except ValueError:
        value = DEFAULT_SYNC_PROXY_INLINE_BUDGET_MS
    return max(0, min(value, MAX_SYNC_PROXY_INLINE_BUDGET_MS))


def _stable_hash(payload: Any) -> str:
    """Return a stable sha256 hash for JSON-like payloads."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _idempotency_key(*, context: SyncProxyInvocationContext, tool_name: str, args_hash: str) -> str:
    """Build an execution-level idempotency key for sync proxy calls."""
    if context.invocation_id or context.function_call_id:
        return ":".join(
            [
                "openppx",
                context.user_id,
                context.session_id,
                context.invocation_id,
                context.function_call_id,
                tool_name,
                args_hash,
            ]
        )
    return f"openppx:{tool_name}:{args_hash}:{os.getpid()}:{uuid.uuid4().hex}"


def _wall_now_ms() -> int:
    """Return current wall-clock time in milliseconds."""
    return int(time.time() * 1000)


def _text(value: Any) -> str:
    """Return a stripped string for optional runtime values."""
    return str(value or "").strip()
