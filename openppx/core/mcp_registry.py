"""MCP toolset construction helpers for openppx."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from typing import Callable

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    SseConnectionParams,
    StdioConnectionParams,
    StreamableHTTPConnectionParams,
)
from loguru import logger
from mcp import StdioServerParameters
from mcp.shared.session import ProgressFnT

from ..runtime.step_events import publish_runtime_step_event
from .env_utils import is_enabled

_MCP_SERVERS_ENV = "OPENPPX_MCP_SERVERS_JSON"
_TRANSIENT_ERROR_HINTS = (
    "timeout",
    "timed out",
    "temporar",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "service unavailable",
    "name or service not known",
    "dns",
    "econnrefused",
    "econnreset",
)
_CONFIG_ERROR_HINTS = (
    "invalid",
    "must be",
    "missing",
    "no such file",
    "permission denied",
    "unauthorized",
    "forbidden",
    "parse",
    "schema",
)


class SafeMcpToolset(McpToolset):
    """MCP toolset that degrades to an empty set on connection errors."""

    async def get_tools(self, *args: Any, **kwargs: Any) -> list[Any]:
        try:
            tools = await super().get_tools(*args, **kwargs)
            mark_available = getattr(self, "mark_available", None)
            if callable(mark_available):
                mark_available()
            return tools
        except Exception as exc:
            mark_unavailable = getattr(self, "mark_unavailable", None)
            if callable(mark_unavailable):
                mark_unavailable(str(exc))
            logger.warning("MCP toolset unavailable; continuing without MCP tools: {}", exc)
            return []


@dataclass(frozen=True)
class McpToolsetMeta:
    """Stable metadata carried by each managed MCP toolset."""

    name: str
    transport: str
    prefix: str


@dataclass(frozen=True)
class McpToolsetOptions:
    """Resolved configurable options for one MCP toolset."""

    tool_filter: list[str] | None
    prefix: str
    require_confirmation: bool
    runtime_headers: dict[str, str]
    progress_events: bool


class ManagedMcpToolset(SafeMcpToolset):
    """Safe MCP toolset with explicit metadata for diagnostics."""

    def __init__(
        self,
        *,
        meta: McpToolsetMeta,
        connection_params: Any,
        tool_filter: list[str] | None,
        require_confirmation: bool,
        header_provider: Callable[[ReadonlyContext], dict[str, str]] | None = None,
        progress_callback: Callable[..., ProgressFnT | None] | ProgressFnT | None = None,
        runtime_headers: dict[str, str] | None = None,
        progress_events: bool = False,
    ) -> None:
        self.meta = meta
        self.runtime_headers = dict(runtime_headers or {})
        self.progress_events = bool(progress_events)
        # Runtime health state is tracked for startup diagnostics and operator hints.
        self.availability_status = "unknown"
        self.availability_message = ""
        super().__init__(
            connection_params=connection_params,
            tool_filter=tool_filter,
            tool_name_prefix=meta.prefix,
            require_confirmation=require_confirmation,
            header_provider=header_provider,
            progress_callback=progress_callback,
        )

    def mark_available(self) -> None:
        """Mark the MCP toolset as reachable in this process."""
        self.availability_status = "available"
        self.availability_message = ""

    def mark_unavailable(self, reason: str) -> None:
        """Mark the MCP toolset as unavailable with a concise reason."""
        self.availability_status = "unavailable"
        self.availability_message = reason.strip()


def _load_servers_from_env() -> dict[str, Any]:
    """Read and parse MCP servers map from environment."""
    raw = os.getenv(_MCP_SERVERS_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        logger.warning("Invalid {} JSON, skipping MCP servers: {}", _MCP_SERVERS_ENV, exc)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("{} must be a JSON object; got {}", _MCP_SERVERS_ENV, type(parsed).__name__)
        return {}
    return parsed


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _safe_header_name(value: Any) -> str:
    """Return a valid simple header name or an empty string."""
    name = str(value or "").strip()
    if not name or any(ch in name for ch in "\r\n:"):
        return ""
    return name


def _header_value(value: Any) -> str:
    """Render one runtime value as a safe HTTP header value."""
    if value is None:
        return ""
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (dict, list, tuple)):
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        rendered = str(value)
    return rendered.replace("\r", " ").replace("\n", " ").strip()


def _pick(raw: dict[str, Any], snake: str, camel: str, default: Any = None) -> Any:
    if snake in raw:
        return raw[snake]
    if camel in raw:
        return raw[camel]
    return default


def _pick_bool(raw: dict[str, Any], snake: str, camel: str, default: bool = False) -> bool:
    """Resolve one boolean-ish config key with snake/camel aliases."""
    return is_enabled(_pick(raw, snake, camel, default), default=default)


def _is_server_enabled(raw_cfg: dict[str, Any]) -> bool:
    """Resolve per-server enabled flag with a default of true."""
    if "enabled" not in raw_cfg:
        return True
    return is_enabled(raw_cfg.get("enabled"), default=False)


def _normalize_runtime_header_bindings(raw_cfg: dict[str, Any]) -> dict[str, str]:
    """Resolve explicit runtime header bindings from one server config."""
    raw_headers = _pick(raw_cfg, "runtime_headers", "runtimeHeaders", {})
    if not isinstance(raw_headers, dict):
        return {}
    bindings: dict[str, str] = {}
    for raw_name, raw_source in raw_headers.items():
        header_name = _safe_header_name(raw_name)
        source = str(raw_source or "").strip()
        if header_name and source:
            bindings[header_name] = source
    return bindings


def _normalize_tool_name_prefix(server_name: str, raw_prefix: Any) -> str:
    """Return the ADK toolset prefix stem.

    ADK adds an underscore between ``tool_name_prefix`` and the MCP tool name.
    Keep openppx config tolerant of older examples that already included the
    separator so MCP tools render as ``mcp_server_tool`` instead of
    ``mcp_server__tool``.
    """
    prefix = str(raw_prefix or "").strip()
    if not prefix:
        prefix = f"mcp_{server_name}"
    return prefix.rstrip("_")


def _metadata_value(ctx: ReadonlyContext, key: str) -> Any:
    run_config = getattr(ctx, "run_config", None)
    metadata = getattr(run_config, "custom_metadata", None) or {}
    if isinstance(metadata, dict):
        return metadata.get(key)
    return None


def _resolve_runtime_header_source(ctx: ReadonlyContext, source: str) -> Any:
    """Resolve one supported runtime header source from ADK ReadonlyContext."""
    normalized = source.strip()
    session = getattr(ctx, "session", None)
    if normalized == "user_id":
        return getattr(ctx, "user_id", "")
    if normalized == "session_id":
        return getattr(session, "id", "")
    if normalized == "app_name":
        return getattr(session, "app_name", "")
    if normalized == "invocation_id":
        return getattr(ctx, "invocation_id", "")
    if normalized == "agent_name":
        return getattr(ctx, "agent_name", "")
    if normalized.startswith("metadata."):
        return _metadata_value(ctx, normalized.removeprefix("metadata."))
    if normalized.startswith("custom_metadata."):
        return _metadata_value(ctx, normalized.removeprefix("custom_metadata."))
    if normalized.startswith("run_metadata."):
        return _metadata_value(ctx, normalized.removeprefix("run_metadata."))
    if normalized.startswith("state."):
        return getattr(ctx, "state", {}).get(normalized.removeprefix("state."))
    if normalized.startswith("session."):
        return getattr(session, normalized.removeprefix("session."), "")
    if normalized.startswith("literal:"):
        return normalized.removeprefix("literal:")
    return None


def _build_header_provider(
    runtime_headers: dict[str, str],
) -> Callable[[ReadonlyContext], dict[str, str]] | None:
    """Build an ADK MCP header provider from explicit runtime bindings."""
    if not runtime_headers:
        return None

    def _provider(ctx: ReadonlyContext) -> dict[str, str]:
        headers: dict[str, str] = {}
        for header_name, source in runtime_headers.items():
            value = _header_value(_resolve_runtime_header_source(ctx, source))
            if value:
                headers[header_name] = value
        return headers

    return _provider


def _format_progress_number(value: float | int | None) -> str:
    if value is None:
        return ""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _format_mcp_progress(tool_name: str, progress: float, total: float | None, message: str | None) -> str:
    detail = f" - {message.strip()}" if isinstance(message, str) and message.strip() else ""
    if total and total > 0:
        percentage = max(0.0, min(100.0, (float(progress) / float(total)) * 100))
        return (
            f"MCP `{tool_name}` progress {percentage:.0f}% "
            f"({_format_progress_number(progress)}/{_format_progress_number(total)}){detail}"
        )
    return f"MCP `{tool_name}` progress {_format_progress_number(progress)}{detail}"


def _build_progress_callback(meta: McpToolsetMeta, enabled: bool) -> Callable[..., ProgressFnT | None] | None:
    """Build an ADK MCP progress callback factory for openppx step events."""
    if not enabled:
        return None

    def _factory(
        tool_name: str,
        *,
        callback_context: Any | None = None,
        **_kwargs: Any,
    ) -> ProgressFnT | None:
        async def _callback(progress: float, total: float | None, message: str | None) -> None:
            invocation_id = str(getattr(callback_context, "invocation_id", "") or "").strip()
            function_call_id = str(getattr(callback_context, "function_call_id", "") or "").strip()
            session = getattr(callback_context, "session", None)
            session_id = str(getattr(session, "id", "") or "").strip()
            content = _format_mcp_progress(tool_name, progress, total, message)
            extra_metadata: dict[str, Any] = {
                "_feedback_origin": "mcp_progress",
                "_mcp_server": meta.name,
                "_mcp_transport": meta.transport,
                "_mcp_progress": progress,
            }
            if total is not None:
                extra_metadata["_mcp_total"] = total
            await publish_runtime_step_event(
                invocation_id=invocation_id or None,
                function_call_id=function_call_id or None,
                step_id=function_call_id or f"mcp:{meta.name}:{tool_name}",
                step_phase="running",
                step_update_kind="progress",
                step_title=tool_name,
                step_kind="tool",
                tool_name=tool_name,
                session_id=session_id or None,
                content=content,
                extra_metadata=extra_metadata,
            )

        return _callback

    return _factory


def _toolset_meta(toolset: SafeMcpToolset) -> dict[str, str]:
    """Extract stable metadata injected on toolset creation."""
    if isinstance(toolset, ManagedMcpToolset):
        return {
            "name": toolset.meta.name,
            "transport": toolset.meta.transport,
            "prefix": toolset.meta.prefix,
            "status": toolset.availability_status,
            "status_message": toolset.availability_message,
        }
    return {
        "name": "unknown",
        "transport": "unknown",
        "prefix": str(getattr(toolset, "tool_name_prefix", "") or ""),
        "status": "unknown",
        "status_message": "",
    }


def summarize_mcp_toolsets(toolsets: list[Any]) -> list[dict[str, str]]:
    """Build a compact summary for MCP toolsets currently attached to the agent."""
    summaries: list[dict[str, str]] = []
    for tool in toolsets:
        if isinstance(tool, SafeMcpToolset):
            summaries.append(_toolset_meta(tool))
    return summaries


async def probe_mcp_toolsets(
    toolsets: list[SafeMcpToolset],
    *,
    timeout_seconds: float = 5.0,
    retry_attempts: int = 1,
    retry_backoff_seconds: float = 0.3,
) -> list[dict[str, Any]]:
    """Probe MCP servers by listing tools, returning per-server health results.

    This call uses strict `McpToolset.get_tools` to surface connection errors.
    Transient failures are retried with exponential backoff.
    """
    timeout = min(max(float(timeout_seconds), 1.0), 30.0)
    attempts_limit = min(max(int(retry_attempts), 1), 5)
    backoff_base = min(max(float(retry_backoff_seconds), 0.0), 5.0)
    return await asyncio.gather(
        *[
            _probe_one_toolset(
                toolset,
                timeout=timeout,
                attempts_limit=attempts_limit,
                backoff_base=backoff_base,
            )
            for toolset in toolsets
        ]
    )


async def _probe_one_toolset(
    toolset: SafeMcpToolset,
    *,
    timeout: float,
    attempts_limit: int,
    backoff_base: float,
) -> dict[str, Any]:
    """Probe one MCP toolset with retry/backoff policy."""
    meta = _toolset_meta(toolset)
    started = time.perf_counter()
    status = "unknown"
    error = ""
    error_kind = ""
    tool_count = 0
    attempts_used = 0
    for attempt in range(1, attempts_limit + 1):
        attempts_used = attempt
        try:
            tools = await asyncio.wait_for(McpToolset.get_tools(toolset), timeout=timeout)
            tool_count = len(tools)
            status = "ok"
            error = ""
            error_kind = ""
            break
        except asyncio.TimeoutError:
            status = "timeout"
            error = f"timed out after {timeout:.1f}s"
            error_kind = "transient"
        except Exception as exc:
            status = "error"
            error = str(exc)
            error_kind = _classify_probe_error(exc)
        if error_kind == "transient" and attempt < attempts_limit:
            delay = backoff_base * (2 ** (attempt - 1))
            if delay > 0:
                await asyncio.sleep(delay)
            continue
        break
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if isinstance(toolset, ManagedMcpToolset):
        if status == "ok":
            toolset.mark_available()
        else:
            detail = error or f"{status}/{error_kind or 'unknown'}"
            toolset.mark_unavailable(detail)
    return {
        "name": meta["name"],
        "transport": meta["transport"],
        "prefix": meta["prefix"],
        "status": status,
        "error_kind": error_kind,
        "tool_count": tool_count,
        "elapsed_ms": elapsed_ms,
        "attempts": attempts_used,
        "error": error,
    }


def _classify_probe_error(exc: Exception) -> str:
    """Classify MCP probe errors for retry and diagnostics decisions."""
    for item in _iter_exception_chain(exc):
        if isinstance(item, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
            return "transient"
        if isinstance(item, (PermissionError, FileNotFoundError, ValueError, TypeError)):
            return "config"
        # Many network stack failures surface as plain OSError.
        if isinstance(item, OSError):
            return "transient"

    message = str(exc).lower()
    if any(hint in message for hint in _CONFIG_ERROR_HINTS):
        return "config"
    if any(hint in message for hint in _TRANSIENT_ERROR_HINTS):
        return "transient"
    return "unknown"


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    """Expand exception, cause, and context chain for robust type matching."""
    items: list[BaseException] = []
    cursor: BaseException | None = exc
    while cursor is not None and cursor not in items:
        items.append(cursor)
        cursor = cursor.__cause__ or cursor.__context__
    return items


def _build_connection_params(server_name: str, raw_cfg: dict[str, Any]) -> tuple[Any, str] | None:
    """Build MCP connection params and transport name from one server config."""
    command = str(raw_cfg.get("command", "") or "").strip()
    url = str(raw_cfg.get("url", "") or "").strip()
    args = _string_list(raw_cfg.get("args", []))
    env = _string_dict(raw_cfg.get("env", {}))
    headers = _string_dict(raw_cfg.get("headers", {})) or None
    transport = str(raw_cfg.get("transport", "") or "").strip().lower()

    if command:
        return (
            StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=command,
                    args=args,
                    env=env or None,
                ),
            ),
            "stdio",
        )
    if url:
        if transport == "sse" or url.lower().rstrip("/").endswith("/sse"):
            return SseConnectionParams(url=url, headers=headers), "sse"
        return StreamableHTTPConnectionParams(url=url, headers=headers), "http"

    logger.warning("MCP server '{}' has neither command nor url; skipping", server_name)
    return None


def _resolve_toolset_options(server_name: str, raw_cfg: dict[str, Any]) -> McpToolsetOptions:
    """Resolve tool filter, name prefix and confirmation options."""
    tool_filter = _pick(raw_cfg, "tool_filter", "toolFilter")
    tool_filter_list = _string_list(tool_filter) if isinstance(tool_filter, list) else None

    prefix = _normalize_tool_name_prefix(server_name, _pick(raw_cfg, "tool_name_prefix", "toolNamePrefix", ""))
    require_confirmation = _pick_bool(raw_cfg, "require_confirmation", "requireConfirmation", False)
    runtime_headers = _normalize_runtime_header_bindings(raw_cfg)
    progress_events = _pick_bool(raw_cfg, "progress_events", "progressEvents", False)
    return McpToolsetOptions(
        tool_filter=tool_filter_list,
        prefix=prefix,
        require_confirmation=require_confirmation,
        runtime_headers=runtime_headers,
        progress_events=progress_events,
    )


def build_mcp_toolsets(mcp_servers: dict[str, Any], *, log_registered: bool = True) -> list[ManagedMcpToolset]:
    """Build configured MCP toolsets.

    Supported per-server config keys:
    - `enabled` (optional, default true)
    - `command` + `args` + `env` (stdio)
    - `url` (+ optional `headers`, `transport=sse|http`)
    - `toolFilter` / `tool_filter`
    - `toolNamePrefix` / `tool_name_prefix`
    - `requireConfirmation` / `require_confirmation`
    - `runtimeHeaders` / `runtime_headers`
    - `progressEvents` / `progress_events`
    """
    toolsets: list[ManagedMcpToolset] = []
    for server_name, raw_cfg in mcp_servers.items():
        if not isinstance(raw_cfg, dict):
            logger.warning("MCP server '{}' config must be an object; got {}", server_name, type(raw_cfg).__name__)
            continue
        if not _is_server_enabled(raw_cfg):
            logger.info("MCP server '{}' disabled via config; skipping", server_name)
            continue

        built = _build_connection_params(str(server_name), raw_cfg)
        if built is None:
            continue
        connection_params, transport_name = built

        options = _resolve_toolset_options(str(server_name), raw_cfg)

        meta = McpToolsetMeta(
            name=str(server_name),
            transport=transport_name,
            prefix=options.prefix,
        )
        toolset = ManagedMcpToolset(
            meta=meta,
            connection_params=connection_params,
            tool_filter=options.tool_filter,
            require_confirmation=options.require_confirmation,
            header_provider=_build_header_provider(options.runtime_headers),
            progress_callback=_build_progress_callback(meta, options.progress_events),
            runtime_headers=options.runtime_headers,
            progress_events=options.progress_events,
        )
        toolsets.append(toolset)
        if log_registered:
            logger.info("MCP server '{}' registered (prefix='{}')", server_name, options.prefix)

    return toolsets


def build_mcp_toolsets_from_env(*, log_registered: bool = True) -> list[ManagedMcpToolset]:
    """Build MCP toolsets from `OPENPPX_MCP_SERVERS_JSON`."""
    return build_mcp_toolsets(_load_servers_from_env(), log_registered=log_registered)
