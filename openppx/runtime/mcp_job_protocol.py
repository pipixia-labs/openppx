"""Explicit MCP external-job protocol support.

This module intentionally does not infer job semantics from arbitrary MCP
results. A server must opt in with a concrete protocol config before openppx
turns a MCP result into a rejoinable external ``TaskRun``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from google.adk.tools.base_tool import BaseTool


DEFAULT_MCP_JOB_POLL_TIMEOUT_MS = 5_000
MAX_MCP_JOB_POLL_TIMEOUT_MS = 60_000
_DEFAULT_JOB_ARGS = {"job_id": "{job_id}"}
_TOOL_REGISTRY: dict[str, dict[str, BaseTool]] = {}
_TOOL_REGISTRY_LOCK = threading.Lock()
_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class McpJobProtocolConfig:
    """Declarative protocol for one MCP server's external job tools."""

    enabled: bool
    job_id_path: str
    status_tool: str
    status_args: dict[str, Any]
    status_result_path: str
    output_tool: str
    output_args: dict[str, Any]
    output_result_path: str
    cancel_tool: str
    cancel_args: dict[str, Any]
    cancel_result_path: str
    poll_timeout_ms: int
    pause_tool: str = ""
    pause_args: dict[str, Any] = field(default_factory=lambda: dict(_DEFAULT_JOB_ARGS))
    pause_result_path: str = ""
    resume_tool: str = ""
    resume_args: dict[str, Any] = field(default_factory=lambda: dict(_DEFAULT_JOB_ARGS))
    resume_result_path: str = ""
    checkpoint_path: str = ""
    checkpoint_schema: str = ""
    checkpoint_schema_version: int | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable protocol payload for task storage."""
        payload = {
            "enabled": self.enabled,
            "job_id_path": self.job_id_path,
            "status_tool": self.status_tool,
            "status_args": self.status_args,
            "status_result_path": self.status_result_path,
            "output_tool": self.output_tool,
            "output_args": self.output_args,
            "output_result_path": self.output_result_path,
            "cancel_tool": self.cancel_tool,
            "cancel_args": self.cancel_args,
            "cancel_result_path": self.cancel_result_path,
            "poll_timeout_ms": self.poll_timeout_ms,
            "pause_tool": self.pause_tool,
            "pause_args": self.pause_args,
            "pause_result_path": self.pause_result_path,
            "resume_tool": self.resume_tool,
            "resume_args": self.resume_args,
            "resume_result_path": self.resume_result_path,
            "checkpoint_path": self.checkpoint_path,
        }
        if self.checkpoint_schema:
            payload["checkpoint_schema"] = self.checkpoint_schema
        if self.checkpoint_schema_version is not None:
            payload["checkpoint_schema_version"] = self.checkpoint_schema_version
        return payload

    @property
    def runner_capabilities(self) -> dict[str, bool]:
        """Return conservative runner capabilities implied by this protocol."""
        return {
            "status": bool(self.status_tool),
            "cancel": bool(self.cancel_tool),
            "interrupt": False,
            "output": bool(self.output_tool),
            "artifact": False,
            "rejoin": bool(self.status_tool),
            "pause": bool(self.pause_tool),
            "checkpoint": bool(self.resume_tool or self.checkpoint_path),
            "resume": bool(self.resume_tool),
        }


@dataclass(frozen=True, slots=True)
class McpJobToolCallResult:
    """Result of invoking a server-specific MCP job control tool."""

    ok: bool
    payload: Any = None
    raw_result: Any = None
    error: str = ""
    missing_tool: bool = False
    timed_out: bool = False


def normalize_mcp_job_protocol(raw: Any) -> McpJobProtocolConfig | None:
    """Parse one per-server MCP job protocol config.

    The config is intentionally strict: without both ``jobIdPath`` and
    ``statusTool`` openppx cannot safely treat a result as a rejoinable job.
    """
    if not isinstance(raw, dict):
        return None
    enabled = _bool(_pick(raw, "enabled", "enabled", True), default=True)
    if not enabled:
        return None
    job_id_path = _text(_pick(raw, "job_id_path", "jobIdPath", ""))
    status_tool = _text(_pick(raw, "status_tool", "statusTool", ""))
    if not job_id_path or not status_tool:
        return None
    status_args = _dict_or_default(_pick(raw, "status_args", "statusArgs", _DEFAULT_JOB_ARGS), _DEFAULT_JOB_ARGS)
    output_tool = _text(_pick(raw, "output_tool", "outputTool", ""))
    output_args = _dict_or_default(_pick(raw, "output_args", "outputArgs", _DEFAULT_JOB_ARGS), _DEFAULT_JOB_ARGS)
    cancel_tool = _text(_pick(raw, "cancel_tool", "cancelTool", ""))
    cancel_args = _dict_or_default(_pick(raw, "cancel_args", "cancelArgs", _DEFAULT_JOB_ARGS), _DEFAULT_JOB_ARGS)
    pause_tool = _text(_pick(raw, "pause_tool", "pauseTool", ""))
    pause_args = _dict_or_default(_pick(raw, "pause_args", "pauseArgs", _DEFAULT_JOB_ARGS), _DEFAULT_JOB_ARGS)
    resume_tool = _text(_pick(raw, "resume_tool", "resumeTool", ""))
    resume_args = _dict_or_default(_pick(raw, "resume_args", "resumeArgs", _DEFAULT_JOB_ARGS), _DEFAULT_JOB_ARGS)
    checkpoint_schema_version = _optional_int(
        _pick(raw, "checkpoint_schema_version", "checkpointSchemaVersion", None)
    )
    return McpJobProtocolConfig(
        enabled=True,
        job_id_path=job_id_path,
        status_tool=status_tool,
        status_args=status_args,
        status_result_path=_text(_pick(raw, "status_result_path", "statusResultPath", "")),
        output_tool=output_tool,
        output_args=output_args,
        output_result_path=_text(_pick(raw, "output_result_path", "outputResultPath", "")),
        cancel_tool=cancel_tool,
        cancel_args=cancel_args,
        cancel_result_path=_text(_pick(raw, "cancel_result_path", "cancelResultPath", "")),
        poll_timeout_ms=normalize_mcp_job_poll_timeout_ms(_pick(raw, "poll_timeout_ms", "pollTimeoutMs", None)),
        pause_tool=pause_tool,
        pause_args=pause_args,
        pause_result_path=_text(_pick(raw, "pause_result_path", "pauseResultPath", "")),
        resume_tool=resume_tool,
        resume_args=resume_args,
        resume_result_path=_text(_pick(raw, "resume_result_path", "resumeResultPath", "")),
        checkpoint_path=_text(_pick(raw, "checkpoint_path", "checkpointPath", "")),
        checkpoint_schema=_text(_pick(raw, "checkpoint_schema", "checkpointSchema", "")),
        checkpoint_schema_version=checkpoint_schema_version,
    )


def mcp_job_protocol_from_payload(raw: Any) -> McpJobProtocolConfig | None:
    """Return a protocol config from a task runner payload value."""
    return normalize_mcp_job_protocol(raw)


def normalize_mcp_job_poll_timeout_ms(value: Any) -> int:
    """Clamp MCP job status/control call timeout."""
    if value is None:
        return DEFAULT_MCP_JOB_POLL_TIMEOUT_MS
    try:
        parsed = int(value)
    except Exception:
        return DEFAULT_MCP_JOB_POLL_TIMEOUT_MS
    return max(100, min(parsed, MAX_MCP_JOB_POLL_TIMEOUT_MS))


def register_mcp_job_tools(server_name: str, tools: list[Any]) -> None:
    """Register current-process MCP ADK tools for job status/output/cancel calls."""
    normalized_server = _text(server_name) or "unknown"
    entries: dict[str, BaseTool] = {}
    for tool in tools:
        if not isinstance(tool, BaseTool):
            continue
        tool_name = _text(getattr(tool, "name", ""))
        if tool_name:
            entries[tool_name] = tool
        raw_mcp_tool = getattr(tool, "raw_mcp_tool", None)
        raw_name = _text(getattr(raw_mcp_tool, "name", ""))
        if raw_name:
            entries[raw_name] = tool
    with _TOOL_REGISTRY_LOCK:
        _TOOL_REGISTRY[normalized_server] = entries


def clear_mcp_job_tools() -> None:
    """Clear the current-process MCP job tool registry. Intended for tests."""
    with _TOOL_REGISTRY_LOCK:
        _TOOL_REGISTRY.clear()


def extract_mcp_job_id(result: Any, protocol: McpJobProtocolConfig | None) -> str:
    """Extract a configured external job id from one MCP tool result."""
    if protocol is None:
        return ""
    return _text(extract_path(result, protocol.job_id_path))


def mcp_job_status_snapshot(result: Any, *, default_status: str = "running") -> dict[str, Any]:
    """Return a normalized status snapshot object from a MCP job result."""
    if isinstance(result, dict):
        snapshot = dict(result)
    else:
        snapshot = {"output": _render_payload(result)}
    if "status" not in snapshot:
        for alias in ("state", "phase"):
            if alias in snapshot:
                snapshot["status"] = snapshot[alias]
                break
    if "status" not in snapshot and default_status:
        snapshot["status"] = default_status
    return snapshot


def normalize_mcp_job_checkpoint_payload(
    *,
    protocol: McpJobProtocolConfig,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Normalize a provider checkpoint using the declared MCP job protocol.

    The remote provider still owns the checkpoint structure. openppx only
    normalizes explicit schema aliases and rejects declared schema conflicts so
    future migrations do not silently interpret the wrong payload version.
    """
    normalized = dict(payload)
    if "schema_version" not in normalized and "schemaVersion" in normalized:
        parsed_alias_version = _optional_int(normalized.get("schemaVersion"))
        if parsed_alias_version is not None:
            normalized["schema_version"] = parsed_alias_version

    declared_schema = _text(protocol.checkpoint_schema)
    if declared_schema:
        existing_schema = _text(normalized.get("schema"))
        if existing_schema and existing_schema != declared_schema:
            raise ValueError(
                f"MCP checkpoint schema mismatch: expected {declared_schema!r}, got {existing_schema!r}"
            )
        normalized["schema"] = declared_schema

    declared_version = protocol.checkpoint_schema_version
    if declared_version is not None:
        existing_version = _optional_int(normalized.get("schema_version"))
        if existing_version is not None and existing_version != declared_version:
            raise ValueError(
                "MCP checkpoint schema_version mismatch: "
                f"expected {declared_version!r}, got {normalized.get('schema_version')!r}"
            )
        normalized["schema_version"] = declared_version
    return normalized


def call_mcp_job_status(
    *,
    server_name: str,
    protocol: McpJobProtocolConfig,
    job_id: str,
    context_payload: dict[str, Any] | None = None,
) -> McpJobToolCallResult:
    """Call the configured MCP job status tool."""
    return _call_mcp_job_tool(
        server_name=server_name,
        tool_name=protocol.status_tool,
        args_template=protocol.status_args,
        result_path=protocol.status_result_path,
        job_id=job_id,
        context_payload=context_payload,
        timeout_ms=protocol.poll_timeout_ms,
    )


def call_mcp_job_output(
    *,
    server_name: str,
    protocol: McpJobProtocolConfig,
    job_id: str,
    context_payload: dict[str, Any] | None = None,
) -> McpJobToolCallResult:
    """Call the configured MCP job output tool."""
    if not protocol.output_tool:
        return McpJobToolCallResult(ok=False, error="MCP job output tool is not configured.", missing_tool=True)
    return _call_mcp_job_tool(
        server_name=server_name,
        tool_name=protocol.output_tool,
        args_template=protocol.output_args,
        result_path=protocol.output_result_path,
        job_id=job_id,
        context_payload=context_payload,
        timeout_ms=protocol.poll_timeout_ms,
    )


def call_mcp_job_cancel(
    *,
    server_name: str,
    protocol: McpJobProtocolConfig,
    job_id: str,
    context_payload: dict[str, Any] | None = None,
) -> McpJobToolCallResult:
    """Call the configured MCP job cancel tool."""
    if not protocol.cancel_tool:
        return McpJobToolCallResult(ok=False, error="MCP job cancel tool is not configured.", missing_tool=True)
    return _call_mcp_job_tool(
        server_name=server_name,
        tool_name=protocol.cancel_tool,
        args_template=protocol.cancel_args,
        result_path=protocol.cancel_result_path,
        job_id=job_id,
        context_payload=context_payload,
        timeout_ms=protocol.poll_timeout_ms,
    )


def call_mcp_job_pause(
    *,
    server_name: str,
    protocol: McpJobProtocolConfig,
    job_id: str,
    context_payload: dict[str, Any] | None = None,
) -> McpJobToolCallResult:
    """Call the configured MCP job pause tool."""
    if not protocol.pause_tool:
        return McpJobToolCallResult(ok=False, error="MCP job pause tool is not configured.", missing_tool=True)
    return _call_mcp_job_tool(
        server_name=server_name,
        tool_name=protocol.pause_tool,
        args_template=protocol.pause_args,
        result_path=protocol.pause_result_path,
        job_id=job_id,
        context_payload=context_payload,
        timeout_ms=protocol.poll_timeout_ms,
    )


def call_mcp_job_resume(
    *,
    server_name: str,
    protocol: McpJobProtocolConfig,
    job_id: str,
    context_payload: dict[str, Any] | None = None,
) -> McpJobToolCallResult:
    """Call the configured MCP job resume tool."""
    if not protocol.resume_tool:
        return McpJobToolCallResult(ok=False, error="MCP job resume tool is not configured.", missing_tool=True)
    return _call_mcp_job_tool(
        server_name=server_name,
        tool_name=protocol.resume_tool,
        args_template=protocol.resume_args,
        result_path=protocol.resume_result_path,
        job_id=job_id,
        context_payload=context_payload,
        timeout_ms=protocol.poll_timeout_ms,
    )


def extract_path(payload: Any, path: str) -> Any:
    """Extract a simple dotted JSON path from a payload."""
    normalized = _text(path)
    if not normalized or normalized == "$":
        return payload
    if normalized.startswith("$."):
        normalized = normalized[2:]
    current = payload
    for part in normalized.split("."):
        key = part.strip()
        if not key:
            continue
        if isinstance(current, dict):
            if key not in current:
                return None
            current = current[key]
            continue
        if isinstance(current, list) and key.isdigit():
            index = int(key)
            if index >= len(current):
                return None
            current = current[index]
            continue
        return None
    return current


def _call_mcp_job_tool(
    *,
    server_name: str,
    tool_name: str,
    args_template: dict[str, Any],
    result_path: str,
    job_id: str,
    context_payload: dict[str, Any] | None,
    timeout_ms: int,
) -> McpJobToolCallResult:
    """Invoke one registered MCP job tool in a bounded worker thread."""
    tool = _lookup_tool(server_name, tool_name)
    if tool is None:
        return McpJobToolCallResult(
            ok=False,
            error=f"MCP job tool {tool_name!r} is not registered for server {server_name!r}.",
            missing_tool=True,
        )
    args = _render_args(args_template, job_id=job_id)
    context = _SyntheticMcpToolContext(context_payload or {})
    future = _executor().submit(_run_mcp_tool_in_new_loop, tool, args, context)
    try:
        raw_result = future.result(timeout=timeout_ms / 1000.0)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return McpJobToolCallResult(
            ok=False,
            error=f"MCP job tool {tool_name!r} timed out after {timeout_ms} ms.",
            timed_out=True,
        )
    except Exception as exc:
        return McpJobToolCallResult(ok=False, error=str(exc) or type(exc).__name__)
    payload = extract_path(raw_result, result_path) if result_path else raw_result
    return McpJobToolCallResult(ok=True, payload=payload, raw_result=raw_result)


def _lookup_tool(server_name: str, tool_name: str) -> BaseTool | None:
    with _TOOL_REGISTRY_LOCK:
        return _TOOL_REGISTRY.get(_text(server_name) or "unknown", {}).get(_text(tool_name))


def _run_mcp_tool_in_new_loop(tool: BaseTool, args: dict[str, Any], context: Any) -> Any:
    """Run an async MCP tool in a private event loop."""
    return asyncio.run(tool.run_async(args=args, tool_context=context))


def _executor() -> concurrent.futures.ThreadPoolExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=_max_workers(),
                thread_name_prefix="openppx-mcp-job-protocol",
            )
        return _EXECUTOR


def _max_workers() -> int:
    raw = os.getenv("OPENPPX_MCP_JOB_PROTOCOL_MAX_WORKERS", "").strip()
    try:
        value = int(raw) if raw else 4
    except ValueError:
        value = 4
    return max(1, min(value, 16))


def _render_args(template: dict[str, Any], *, job_id: str) -> dict[str, Any]:
    rendered = _render_template(template, job_id=job_id)
    return rendered if isinstance(rendered, dict) else dict(_DEFAULT_JOB_ARGS)


def _render_template(value: Any, *, job_id: str) -> Any:
    if isinstance(value, dict):
        return {str(k): _render_template(v, job_id=job_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_template(item, job_id=job_id) for item in value]
    if isinstance(value, str):
        return value.replace("{job_id}", job_id).replace("{external_ref}", job_id)
    return value


class _SyntheticMcpToolContext:
    """Small ToolContext-like object for background MCP job control calls."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.user_id = _text(payload.get("user_id"))
        self.invocation_id = _text(payload.get("invocation_id"))
        self.function_call_id = _text(payload.get("function_call_id"))
        self.tool_confirmation = None
        session_id = _text(payload.get("session_id"))
        self.session = SimpleNamespace(
            id=session_id,
            user_id=self.user_id,
            app_name=_text(payload.get("app_name")) or "openppx",
            state=dict(payload.get("state") if isinstance(payload.get("state"), dict) else {}),
        )
        self.state = self.session.state
        self._invocation_context = SimpleNamespace(
            user_id=self.user_id,
            invocation_id=self.invocation_id,
            session=self.session,
            agent=SimpleNamespace(name=_text(payload.get("agent_name")) or "openppx"),
            run_config=SimpleNamespace(custom_metadata=dict(payload.get("custom_metadata") or {})),
            user_content=None,
            credential_by_key={},
        )

    def request_confirmation(self, **_kwargs: Any) -> None:
        """Ignore confirmation requests in background job maintenance."""
        return None

    def render_ui_widget(self, *_args: Any, **_kwargs: Any) -> None:
        """Ignore UI widget rendering in background job maintenance."""
        return None


def _pick(raw: dict[str, Any], snake: str, camel: str, default: Any = None) -> Any:
    if snake in raw:
        return raw[snake]
    if camel in raw:
        return raw[camel]
    return default


def _dict_or_default(value: Any, default: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return dict(default)


def _bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _render_payload(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)
