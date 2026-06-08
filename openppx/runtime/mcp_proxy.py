"""Runtime-length MCP tool proxy support."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from google.adk.tools.base_tool import BaseTool

from .mcp_job_protocol import McpJobProtocolConfig
from .mcp_job_protocol import extract_mcp_job_id
from .mcp_job_protocol import mcp_job_protocol_from_payload
from .mcp_job_protocol import mcp_job_status_snapshot
from .task_store import (
    TaskEventStore,
    TaskStore,
    ToolCallRecordStore,
)


DEFAULT_MCP_PROXY_INLINE_BUDGET_MS = 5_000
MAX_MCP_PROXY_INLINE_BUDGET_MS = 120_000
MAX_MCP_PROXY_SUMMARY_CHARS = 4_000
MCP_JOB_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "lost"})
MCP_PROXY_RUNNER_CAPABILITIES: dict[str, bool] = {
    "status": True,
    "cancel": True,
    "interrupt": True,
    "output": True,
    "artifact": False,
    "rejoin": True,
    "pause": False,
    "checkpoint": False,
}

_BACKGROUND_TASKS: dict[str, asyncio.Task[Any]] = {}
_BACKGROUND_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class McpProxyInvocationContext:
    """Metadata that binds one MCP proxy call to the current ADK invocation."""

    user_id: str = ""
    session_id: str = ""
    thread_id: str = ""
    turn_id: str = ""
    invocation_id: str = ""
    function_call_id: str = ""
    tool_call_id: str = ""
    owner_key: str = ""


class McpLongTaskProxyTool(BaseTool):
    """ADK tool wrapper that turns slow MCP calls into openppx TaskRun facts."""

    _openppx_mcp_proxy = True

    def __init__(
        self,
        *,
        wrapped_tool: BaseTool,
        server_name: str,
        transport: str,
        inline_budget_ms: int | None = None,
        job_protocol: McpJobProtocolConfig | None = None,
    ) -> None:
        self._wrapped_tool = wrapped_tool
        self._server_name = str(server_name or "").strip() or "unknown"
        self._transport = str(transport or "").strip() or "unknown"
        self._inline_budget_ms = normalize_mcp_proxy_inline_budget_ms(inline_budget_ms)
        self._job_protocol = job_protocol
        super().__init__(
            name=str(getattr(wrapped_tool, "name", "") or ""),
            description=str(getattr(wrapped_tool, "description", "") or ""),
            is_long_running=bool(getattr(wrapped_tool, "is_long_running", False)),
            custom_metadata=_merge_custom_metadata(
                getattr(wrapped_tool, "custom_metadata", None),
                server_name=self._server_name,
                transport=self._transport,
                inline_budget_ms=self._inline_budget_ms,
                job_protocol=self._job_protocol,
            ),
        )

    @property
    def wrapped_tool(self) -> BaseTool:
        """Return the wrapped ADK MCP tool."""
        return self._wrapped_tool

    @property
    def raw_mcp_tool(self) -> Any:
        """Expose raw MCP metadata for existing diagnostics/CLI collectors."""
        return getattr(self._wrapped_tool, "raw_mcp_tool", None)

    def __getattr__(self, name: str) -> Any:
        """Delegate MCP-specific attributes to the wrapped ADK tool."""
        return getattr(self._wrapped_tool, name)

    def _get_declaration(self) -> Any:
        """Return the wrapped tool's original function declaration."""
        get_declaration = getattr(self._wrapped_tool, "_get_declaration", None)
        if callable(get_declaration):
            return get_declaration()
        return None

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        """Run the wrapped MCP tool inline or expose a background TaskRun."""
        return await run_mcp_tool_with_proxy(
            wrapped_tool=self._wrapped_tool,
            server_name=self._server_name,
            transport=self._transport,
            args=args,
            tool_context=tool_context,
            inline_budget_ms=self._inline_budget_ms,
            job_protocol=self._job_protocol,
        )


def wrap_mcp_tool_for_long_tasks(
    tool: Any,
    *,
    server_name: str,
    transport: str,
    inline_budget_ms: int | None,
    job_protocol: McpJobProtocolConfig | None = None,
) -> Any:
    """Wrap one ADK MCP tool when it has raw MCP metadata."""
    if getattr(tool, "_openppx_mcp_proxy", False):
        return tool
    if not isinstance(tool, BaseTool):
        return tool
    if getattr(tool, "raw_mcp_tool", None) is None:
        return tool
    return McpLongTaskProxyTool(
        wrapped_tool=tool,
        server_name=server_name,
        transport=transport,
        inline_budget_ms=inline_budget_ms,
        job_protocol=job_protocol,
    )


async def run_mcp_tool_with_proxy(
    *,
    wrapped_tool: BaseTool,
    server_name: str,
    transport: str,
    args: dict[str, Any],
    tool_context: Any,
    inline_budget_ms: int | None,
    job_protocol: McpJobProtocolConfig | None = None,
    task_store: TaskStore | None = None,
    event_store: TaskEventStore | None = None,
    tool_call_store: ToolCallRecordStore | None = None,
) -> Any:
    """Run one MCP tool with runtime-length detection.

    The underlying ADK MCP call starts immediately. If it completes inside the
    inline budget, the original result is returned unchanged. If it is still
    running after the budget, openppx returns a task payload while the same
    coroutine continues in the current process and updates TaskRun facts later.
    """
    resolved_budget_ms = normalize_mcp_proxy_inline_budget_ms(inline_budget_ms)
    stores = _McpProxyStores.from_optional(
        task_store=task_store,
        event_store=event_store,
        tool_call_store=tool_call_store,
    )
    context = _context_from_tool_context(tool_context)
    tool_name = str(getattr(wrapped_tool, "name", "") or "mcp_tool")
    args_hash = _stable_hash({"server": server_name, "tool": tool_name, "args": args})
    idempotency_key = _idempotency_key(context=context, tool_name=tool_name, args_hash=args_hash)
    record, created = stores.tool_call_store.create_or_get(
        idempotency_key=idempotency_key,
        tool_name=tool_name,
        args_hash=args_hash,
    )
    if not created:
        replayed = _replay_mcp_proxy_record(record, stores=stores)
        if replayed is not None:
            return replayed

    call_task = asyncio.create_task(wrapped_tool.run_async(args=args, tool_context=tool_context))
    done, _pending = await asyncio.wait({call_task}, timeout=resolved_budget_ms / 1000.0)
    if call_task in done:
        return _settle_inline_mcp_call(
            call_task,
            stores=stores,
            idempotency_key=idempotency_key,
            context=context,
            server_name=server_name,
            transport=transport,
            tool_name=tool_name,
            args_hash=args_hash,
            job_protocol=job_protocol,
        )

    task = _materialize_mcp_proxy_task(
        stores=stores,
        context=context,
        server_name=server_name,
        transport=transport,
        tool_name=tool_name,
        args_hash=args_hash,
        idempotency_key=idempotency_key,
        inline_budget_ms=resolved_budget_ms,
        job_protocol=job_protocol,
    )
    _register_background_task(task.task_id, call_task)
    asyncio.create_task(
        _finalize_mcp_proxy_task(
            task_id=task.task_id,
            call_task=call_task,
            stores=stores,
            idempotency_key=idempotency_key,
        )
    )
    return _task_started_payload(task, replayed=False)


def is_mcp_proxy_task_active(task_id: str) -> bool:
    """Return whether a background MCP proxy coroutine is attached here."""
    with _BACKGROUND_LOCK:
        task = _BACKGROUND_TASKS.get(str(task_id))
    return bool(task is not None and not task.done())


def cancel_mcp_proxy_task(task_id: str) -> bool:
    """Cancel a current-process MCP proxy coroutine if it is still attached."""
    with _BACKGROUND_LOCK:
        task = _BACKGROUND_TASKS.get(str(task_id))
    if task is None or task.done():
        return False
    task.cancel()
    return True


def normalize_mcp_proxy_inline_budget_ms(value: int | None) -> int:
    """Clamp MCP proxy inline wait budget to safe bounds."""
    if value is None:
        return DEFAULT_MCP_PROXY_INLINE_BUDGET_MS
    try:
        parsed = int(value)
    except Exception:
        return DEFAULT_MCP_PROXY_INLINE_BUDGET_MS
    return max(0, min(parsed, MAX_MCP_PROXY_INLINE_BUDGET_MS))


@dataclass(frozen=True, slots=True)
class _McpProxyStores:
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
    ) -> "_McpProxyStores":
        resolved_task_store = task_store or TaskStore()
        return cls(
            task_store=resolved_task_store,
            event_store=event_store or TaskEventStore(db_path=resolved_task_store.db_path),
            tool_call_store=tool_call_store or ToolCallRecordStore(db_path=resolved_task_store.db_path),
        )


def _context_from_tool_context(tool_context: Any) -> McpProxyInvocationContext:
    """Extract stable invocation metadata from ADK ToolContext-like objects."""
    session = getattr(tool_context, "session", None)
    invocation_id = _text(getattr(tool_context, "invocation_id", ""))
    if not invocation_id:
        invocation_context = getattr(tool_context, "_invocation_context", None)
        invocation_id = _text(getattr(invocation_context, "invocation_id", ""))
    function_call_id = _text(getattr(tool_context, "function_call_id", ""))
    session_id = _text(getattr(session, "id", ""))
    user_id = _text(getattr(tool_context, "user_id", "")) or _text(getattr(session, "user_id", ""))
    thread_id = session_id
    return McpProxyInvocationContext(
        user_id=user_id,
        session_id=session_id,
        thread_id=thread_id,
        turn_id=invocation_id,
        invocation_id=invocation_id,
        function_call_id=function_call_id,
        tool_call_id=function_call_id,
        owner_key=user_id,
    )


def _settle_inline_mcp_call(
    call_task: asyncio.Task[Any],
    *,
    stores: _McpProxyStores,
    idempotency_key: str,
    context: McpProxyInvocationContext,
    server_name: str,
    transport: str,
    tool_name: str,
    args_hash: str,
    job_protocol: McpJobProtocolConfig | None,
) -> Any:
    """Return a completed inline MCP result while recording idempotency facts."""
    try:
        result = call_task.result()
    except Exception as exc:
        stores.tool_call_store.settle(idempotency_key, status="failed", error=str(exc))
        raise
    job_task = _maybe_materialize_mcp_job_task(
        stores=stores,
        context=context,
        server_name=server_name,
        transport=transport,
        tool_name=tool_name,
        args_hash=args_hash,
        idempotency_key=idempotency_key,
        job_protocol=job_protocol,
        result=result,
    )
    if job_task is not None:
        return _task_started_payload(job_task, replayed=False)
    status, error = _status_for_mcp_result(result)
    stores.tool_call_store.settle(
        idempotency_key,
        status=status,
        result={"tool_result": result},
        error=error,
    )
    return result


def _materialize_mcp_proxy_task(
    *,
    stores: _McpProxyStores,
    context: McpProxyInvocationContext,
    server_name: str,
    transport: str,
    tool_name: str,
    args_hash: str,
    idempotency_key: str,
    inline_budget_ms: int,
    job_protocol: McpJobProtocolConfig | None,
) -> Any:
    """Create TaskRun facts for one MCP call that exceeded inline budget."""
    progress = f"MCP tool `{tool_name}` is still running after {inline_budget_ms} ms."
    started_at_ms = _wall_now_ms()
    task = stores.task_store.create_task(
        kind="mcp",
        status="running",
        title=f"MCP {server_name}:{tool_name}",
        owner_key=context.owner_key or context.user_id,
        user_id=context.user_id,
        thread_id=context.thread_id or context.session_id,
        session_id=context.session_id,
        turn_id=context.turn_id or context.invocation_id,
        invocation_id=context.invocation_id,
        function_call_id=context.function_call_id,
        tool_call_id=context.tool_call_id,
        dedupe_key=f"mcp:{server_name}:{tool_name}:{args_hash}",
        external_ref="",
        runner_payload=_mcp_proxy_runner_payload(
            server_name=server_name,
            transport=transport,
            tool_name=tool_name,
            inline_budget_ms=inline_budget_ms,
            started_at_ms=started_at_ms,
            progress=progress,
            job_protocol=job_protocol,
            context=context,
        ),
        runner_capabilities=MCP_PROXY_RUNNER_CAPABILITIES,
        resume_policy="rejoin",
        stop_policy="cancel_async_task",
        cancel_policy="cancel_async_task",
        progress_summary=progress,
    )
    stores.event_store.append_event(
        task.task_id,
        "task.started",
        message=progress,
        payload={
            "runner": "mcp_proxy",
            "server": server_name,
            "transport": transport,
            "tool_name": tool_name,
            "inline_budget_ms": inline_budget_ms,
        },
    )
    stores.tool_call_store.link_task(idempotency_key, task.task_id, status="running")
    return task


def _mcp_proxy_runner_payload(
    *,
    server_name: str,
    transport: str,
    tool_name: str,
    inline_budget_ms: int,
    started_at_ms: int,
    progress: str,
    job_protocol: McpJobProtocolConfig | None,
    context: McpProxyInvocationContext,
) -> dict[str, Any]:
    """Return runner payload for a current-process MCP proxy task."""
    payload: dict[str, Any] = {
        "runner": "mcp_proxy",
        "server": server_name,
        "transport": transport,
        "tool_name": tool_name,
        "inline_budget_ms": inline_budget_ms,
        "started_at_ms": started_at_ms,
        "job_context": _mcp_job_context_payload(context),
        "status_snapshot": {
            "status": "running",
            "message": progress,
        },
    }
    if job_protocol is not None:
        payload["job_protocol"] = job_protocol.to_payload()
    return payload


def _maybe_materialize_mcp_job_task(
    *,
    stores: _McpProxyStores,
    context: McpProxyInvocationContext,
    server_name: str,
    transport: str,
    tool_name: str,
    args_hash: str,
    idempotency_key: str,
    job_protocol: McpJobProtocolConfig | None,
    result: Any,
) -> Any | None:
    """Create an external MCP job TaskRun when a configured job id is present."""
    if job_protocol is None:
        return None
    job_id = extract_mcp_job_id(result, job_protocol)
    if not job_id:
        return None
    snapshot = mcp_job_status_snapshot(result, default_status="running")
    status = _normalize_mcp_job_status(snapshot.get("status")) or "running"
    progress = _mcp_job_progress_summary(server_name=server_name, tool_name=tool_name, job_id=job_id, snapshot=snapshot)
    task = stores.task_store.create_task(
        kind="mcp",
        status=status,
        title=f"MCP job {server_name}:{tool_name}",
        owner_key=context.owner_key or context.user_id,
        user_id=context.user_id,
        thread_id=context.thread_id or context.session_id,
        session_id=context.session_id,
        turn_id=context.turn_id or context.invocation_id,
        invocation_id=context.invocation_id,
        function_call_id=context.function_call_id,
        tool_call_id=context.tool_call_id,
        dedupe_key=f"mcp-job:{server_name}:{tool_name}:{args_hash}",
        external_ref=job_id,
        runner_payload=_mcp_job_runner_payload(
            server_name=server_name,
            transport=transport,
            tool_name=tool_name,
            job_id=job_id,
            job_protocol=job_protocol,
            snapshot=snapshot,
            submit_result=result,
            context_payload=_mcp_job_context_payload(context),
        ),
        runner_capabilities=job_protocol.runner_capabilities,
        resume_policy="rejoin",
        stop_policy="not_stoppable",
        cancel_policy="provider_cancel" if job_protocol.cancel_tool else "unsupported",
        progress_summary=progress,
        terminal_summary=_mcp_job_terminal_summary(snapshot) if status in MCP_JOB_TERMINAL_STATUSES else "",
        last_error=_mcp_job_error(snapshot) if status == "failed" else "",
    )
    _settle_mcp_job_tool_call(stores=stores, idempotency_key=idempotency_key, task=task, result=result)
    stores.event_store.append_event(
        task.task_id,
        _mcp_job_event_type(status),
        message=task.terminal_summary or progress,
        payload={
            "runner": "mcp",
            "server": server_name,
            "transport": transport,
            "tool_name": tool_name,
            "external_ref": job_id,
            "snapshot": snapshot,
        },
    )
    return task


def _maybe_transition_proxy_task_to_mcp_job(
    *,
    stores: _McpProxyStores,
    task: Any,
    result: Any,
    idempotency_key: str,
) -> Any | None:
    """Turn an existing MCP proxy TaskRun into an external MCP job TaskRun."""
    payload = task.runner_payload
    job_protocol = mcp_job_protocol_from_payload(payload.get("job_protocol"))
    if job_protocol is None:
        return None
    job_id = extract_mcp_job_id(result, job_protocol)
    if not job_id:
        return None
    server_name = str(payload.get("server", "") or "unknown")
    transport = str(payload.get("transport", "") or "unknown")
    tool_name = str(payload.get("tool_name", "") or "mcp_tool")
    snapshot = mcp_job_status_snapshot(result, default_status="running")
    status = _normalize_mcp_job_status(snapshot.get("status")) or "running"
    progress = _mcp_job_progress_summary(server_name=server_name, tool_name=tool_name, job_id=job_id, snapshot=snapshot)
    updates: dict[str, Any] = {
        "status": status,
        "external_ref": job_id,
        "progress_summary": progress,
        "terminal_summary": _mcp_job_terminal_summary(snapshot) if status in MCP_JOB_TERMINAL_STATUSES else "",
        "last_error": _mcp_job_error(snapshot) if status == "failed" else "",
        "resume_policy": "rejoin",
        "stop_policy": "not_stoppable",
        "cancel_policy": "provider_cancel" if job_protocol.cancel_tool else "unsupported",
    }
    updated = stores.task_store.update_task(
        task.task_id,
        runner_payload=_mcp_job_runner_payload(
            server_name=server_name,
            transport=transport,
            tool_name=tool_name,
            job_id=job_id,
            job_protocol=job_protocol,
            snapshot=snapshot,
            submit_result=result,
            context_payload=dict(payload.get("job_context") if isinstance(payload.get("job_context"), dict) else {}),
        ),
        runner_capabilities=job_protocol.runner_capabilities,
        **updates,
    )
    if updated is None:
        return stores.task_store.get_task(task.task_id)
    _settle_mcp_job_tool_call(stores=stores, idempotency_key=idempotency_key, task=updated, result=result)
    stores.event_store.append_event(
        updated.task_id,
        _mcp_job_event_type(status),
        message=updated.terminal_summary or progress,
        payload={
            "runner": "mcp",
            "server": server_name,
            "transport": transport,
            "tool_name": tool_name,
            "external_ref": job_id,
            "snapshot": snapshot,
        },
    )
    return updated


def _mcp_job_runner_payload(
    *,
    server_name: str,
    transport: str,
    tool_name: str,
    job_id: str,
    job_protocol: McpJobProtocolConfig,
    snapshot: dict[str, Any],
    submit_result: Any,
    context_payload: dict[str, Any],
) -> dict[str, Any]:
    """Return runner payload for an external MCP job task."""
    return {
        "runner": "mcp",
        "server": server_name,
        "transport": transport,
        "tool_name": tool_name,
        "job_id": job_id,
        "job_protocol": job_protocol.to_payload(),
        "job_context": context_payload,
        "status_snapshot": snapshot,
        "submit_result": submit_result,
    }


def _mcp_job_context_payload(context: McpProxyInvocationContext) -> dict[str, Any]:
    """Return minimal context for background MCP job status/control calls."""
    return {
        "user_id": context.user_id,
        "session_id": context.session_id,
        "invocation_id": context.invocation_id,
        "function_call_id": context.function_call_id,
    }


def _settle_mcp_job_tool_call(
    *,
    stores: _McpProxyStores,
    idempotency_key: str,
    task: Any,
    result: Any,
) -> None:
    """Settle or link the tool-call record for an external MCP job."""
    if task.status in MCP_JOB_TERMINAL_STATUSES:
        stores.tool_call_store.settle(
            idempotency_key,
            status=task.status,
            result={"mode": "task", "status": task.status, "task_id": task.task_id, "tool_result": result},
            error=task.last_error,
        )
        return
    stores.tool_call_store.link_task(idempotency_key, task.task_id, status="running")


def _mcp_job_event_type(status: str) -> str:
    if status in MCP_JOB_TERMINAL_STATUSES:
        return f"task.{status}"
    return "task.started"


def _normalize_mcp_job_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "queued": "queued",
        "pending": "queued",
        "created": "queued",
        "running": "running",
        "in_progress": "running",
        "processing": "running",
        "completed": "completed",
        "complete": "completed",
        "succeeded": "completed",
        "success": "completed",
        "failed": "failed",
        "error": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "lost": "lost",
        "stale": "stale",
    }
    return aliases.get(raw, "")


def _mcp_job_progress_summary(
    *,
    server_name: str,
    tool_name: str,
    job_id: str,
    snapshot: dict[str, Any],
) -> str:
    summary = _text(snapshot.get("progress_summary") or snapshot.get("message") or snapshot.get("summary"))
    if summary:
        return summary
    status = _normalize_mcp_job_status(snapshot.get("status")) or "running"
    return f"MCP job `{server_name}:{tool_name}` ({job_id}) is {status}."


def _mcp_job_terminal_summary(snapshot: dict[str, Any]) -> str:
    return _text(
        snapshot.get("terminal_summary")
        or snapshot.get("output")
        or snapshot.get("message")
        or snapshot.get("summary")
        or snapshot.get("error")
    )


def _mcp_job_error(snapshot: dict[str, Any]) -> str:
    return _text(snapshot.get("last_error") or snapshot.get("error") or snapshot.get("message"))


async def _finalize_mcp_proxy_task(
    *,
    task_id: str,
    call_task: asyncio.Task[Any],
    stores: _McpProxyStores,
    idempotency_key: str,
) -> None:
    """Update TaskRun facts when a background MCP coroutine finishes."""
    try:
        result = await call_task
    except asyncio.CancelledError:
        _settle_cancelled_background_task(
            task_id=task_id,
            stores=stores,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        _settle_failed_background_task(
            task_id=task_id,
            stores=stores,
            idempotency_key=idempotency_key,
            exc=exc,
        )
    else:
        _settle_completed_background_task(
            task_id=task_id,
            stores=stores,
            idempotency_key=idempotency_key,
            result=result,
        )
    finally:
        _unregister_background_task(task_id, call_task)


def _settle_completed_background_task(
    *,
    task_id: str,
    stores: _McpProxyStores,
    idempotency_key: str,
    result: Any,
) -> None:
    """Persist background MCP completion or MCP-level failure."""
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
    transitioned = _maybe_transition_proxy_task_to_mcp_job(
        stores=stores,
        task=current,
        result=result,
        idempotency_key=idempotency_key,
    )
    if transitioned is not None:
        return
    status, error = _status_for_mcp_result(result)
    summary = _summarize_mcp_result(result)
    updates: dict[str, Any] = {
        "status": status,
        "terminal_summary": summary,
        "progress_summary": summary,
        "last_error": error,
        "resume_policy": "not_resumable",
    }
    updated = stores.task_store.update_task(task_id, **updates)
    stores.tool_call_store.settle(
        idempotency_key,
        status=status,
        result={
            "mode": "task",
            "status": status,
            "task_id": task_id,
            "tool_result": result,
        },
        error=error,
    )
    if updated is not None:
        event_type = "task.completed" if status == "completed" else "task.failed"
        stores.event_store.append_event(
            task_id,
            event_type,
            message=summary,
            payload={"runner": "mcp_proxy", "error": error},
        )


def _settle_failed_background_task(
    *,
    task_id: str,
    stores: _McpProxyStores,
    idempotency_key: str,
    exc: Exception,
) -> None:
    """Persist an exception raised by a background MCP coroutine."""
    current = stores.task_store.get_task(task_id)
    error = str(exc)
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
    summary = error or type(exc).__name__
    updated = stores.task_store.update_task(
        task_id,
        status="failed",
        terminal_summary=summary,
        progress_summary=summary,
        last_error=summary,
        resume_policy="not_resumable",
    )
    stores.tool_call_store.settle(idempotency_key, status="failed", error=summary)
    if updated is not None:
        stores.event_store.append_event(
            task_id,
            "task.failed",
            message=summary,
            payload={"runner": "mcp_proxy", "error_type": type(exc).__name__},
        )


def _settle_cancelled_background_task(
    *,
    task_id: str,
    stores: _McpProxyStores,
    idempotency_key: str,
) -> None:
    """Settle a background MCP coroutine that was cancelled."""
    current = stores.task_store.get_task(task_id)
    if current is None:
        stores.tool_call_store.settle(idempotency_key, status="interrupted", error="MCP proxy task cancelled.")
        return
    if current.status != "running":
        stores.tool_call_store.settle(
            idempotency_key,
            status=current.status,
            result={"mode": "task", "status": current.status, "task_id": task_id},
            error=current.last_error,
        )
        return
    summary = "MCP proxy task interrupted."
    updated = stores.task_store.update_task(
        task_id,
        status="interrupted",
        terminal_summary=summary,
        progress_summary=summary,
        last_error=summary,
        resume_policy="not_resumable",
    )
    stores.tool_call_store.settle(idempotency_key, status="interrupted", error=summary)
    if updated is not None:
        stores.event_store.append_event(
            task_id,
            "task.interrupted",
            message=summary,
            payload={"runner": "mcp_proxy"},
        )


def _replay_mcp_proxy_record(record: Any, *, stores: _McpProxyStores) -> Any | None:
    """Return a replay payload for duplicate MCP proxy invocations when safe."""
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
    """Return the ADK tool result for a materialized MCP proxy task."""
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


def _status_for_mcp_result(result: Any) -> tuple[str, str]:
    """Classify a MCP result as completed or failed for TaskRun facts."""
    if isinstance(result, dict):
        if bool(result.get("isError")):
            message = result.get("error") or result.get("message") or "MCP tool reported an error."
            return "failed", _text(message)
        error = result.get("error")
        if isinstance(error, str) and error.strip():
            return "failed", error.strip()
    return "completed", ""


def _summarize_mcp_result(result: Any) -> str:
    """Return a compact JSON/text summary for one MCP result."""
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        text = str(result)
    text = text.strip() or "(no MCP result)"
    if len(text) <= MAX_MCP_PROXY_SUMMARY_CHARS:
        return text
    keep = max(500, MAX_MCP_PROXY_SUMMARY_CHARS - 80)
    return f"MCP result exceeded summary budget ({len(text)} chars).\n\nTail:\n{text[-keep:]}"


def _merge_custom_metadata(
    original: Any,
    *,
    server_name: str,
    transport: str,
    inline_budget_ms: int,
    job_protocol: McpJobProtocolConfig | None = None,
) -> dict[str, Any]:
    """Merge proxy metadata into the wrapped tool's metadata."""
    metadata = dict(original) if isinstance(original, dict) else {}
    metadata["openppx_mcp_proxy"] = {
        "server": server_name,
        "transport": transport,
        "inline_budget_ms": inline_budget_ms,
    }
    if job_protocol is not None:
        metadata["openppx_mcp_proxy"]["job_protocol"] = {
            "enabled": True,
            "job_id_path": job_protocol.job_id_path,
            "status_tool": job_protocol.status_tool,
            "output_tool": job_protocol.output_tool,
            "cancel_tool": job_protocol.cancel_tool,
        }
    return metadata


def _register_background_task(task_id: str, task: asyncio.Task[Any]) -> None:
    """Attach one background asyncio task to a TaskRun id."""
    with _BACKGROUND_LOCK:
        _BACKGROUND_TASKS[task_id] = task


def _unregister_background_task(task_id: str, task: asyncio.Task[Any]) -> None:
    """Detach a background asyncio task if it is still the current handle."""
    with _BACKGROUND_LOCK:
        if _BACKGROUND_TASKS.get(task_id) is task:
            _BACKGROUND_TASKS.pop(task_id, None)


def _stable_hash(payload: Any) -> str:
    """Return a stable sha256 hash for JSON-like payloads."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _idempotency_key(*, context: McpProxyInvocationContext, tool_name: str, args_hash: str) -> str:
    """Build an execution-level idempotency key for MCP proxy calls."""
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
