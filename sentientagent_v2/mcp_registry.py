"""MCP toolset construction helpers for sentientagent_v2."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    SseConnectionParams,
    StdioConnectionParams,
    StreamableHTTPConnectionParams,
)
from loguru import logger
from mcp import StdioServerParameters

_MCP_SERVERS_ENV = "SENTIENTAGENT_V2_MCP_SERVERS_JSON"
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
            return await super().get_tools(*args, **kwargs)
        except Exception as exc:
            logger.warning("MCP toolset unavailable; continuing without MCP tools: {}", exc)
            return []


@dataclass(frozen=True)
class McpToolsetMeta:
    """Stable metadata carried by each managed MCP toolset."""

    name: str
    transport: str
    prefix: str


class ManagedMcpToolset(SafeMcpToolset):
    """Safe MCP toolset with explicit metadata for diagnostics."""

    def __init__(
        self,
        *,
        meta: McpToolsetMeta,
        connection_params: Any,
        tool_filter: list[str] | None,
        require_confirmation: bool,
    ) -> None:
        self.meta = meta
        super().__init__(
            connection_params=connection_params,
            tool_filter=tool_filter,
            tool_name_prefix=meta.prefix,
            require_confirmation=require_confirmation,
        )


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


def _pick(raw: dict[str, Any], snake: str, camel: str, default: Any = None) -> Any:
    if snake in raw:
        return raw[snake]
    if camel in raw:
        return raw[camel]
    return default


def _toolset_meta(toolset: SafeMcpToolset) -> dict[str, str]:
    """Extract stable metadata injected on toolset creation."""
    if isinstance(toolset, ManagedMcpToolset):
        return {
            "name": toolset.meta.name,
            "transport": toolset.meta.transport,
            "prefix": toolset.meta.prefix,
        }
    return {
        "name": "unknown",
        "transport": "unknown",
        "prefix": str(getattr(toolset, "tool_name_prefix", "") or ""),
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


def _resolve_toolset_options(server_name: str, raw_cfg: dict[str, Any]) -> tuple[list[str] | None, str, bool]:
    """Resolve tool filter, name prefix and confirmation options."""
    tool_filter = _pick(raw_cfg, "tool_filter", "toolFilter")
    tool_filter_list = _string_list(tool_filter) if isinstance(tool_filter, list) else None

    prefix = str(_pick(raw_cfg, "tool_name_prefix", "toolNamePrefix", "") or "").strip()
    if not prefix:
        prefix = f"mcp_{server_name}_"
    require_confirmation = bool(_pick(raw_cfg, "require_confirmation", "requireConfirmation", False))
    return tool_filter_list, prefix, require_confirmation


def build_mcp_toolsets(mcp_servers: dict[str, Any], *, log_registered: bool = True) -> list[ManagedMcpToolset]:
    """Build configured MCP toolsets.

    Supported per-server config keys:
    - `command` + `args` + `env` (stdio)
    - `url` (+ optional `headers`, `transport=sse|http`)
    - `toolFilter` / `tool_filter`
    - `toolNamePrefix` / `tool_name_prefix`
    - `requireConfirmation` / `require_confirmation`
    """
    toolsets: list[ManagedMcpToolset] = []
    for server_name, raw_cfg in mcp_servers.items():
        if not isinstance(raw_cfg, dict):
            logger.warning("MCP server '{}' config must be an object; got {}", server_name, type(raw_cfg).__name__)
            continue

        built = _build_connection_params(str(server_name), raw_cfg)
        if built is None:
            continue
        connection_params, transport_name = built

        tool_filter_list, prefix, require_confirmation = _resolve_toolset_options(str(server_name), raw_cfg)

        meta = McpToolsetMeta(
            name=str(server_name),
            transport=transport_name,
            prefix=prefix,
        )
        toolset = ManagedMcpToolset(
            meta=meta,
            connection_params=connection_params,
            tool_filter=tool_filter_list,
            require_confirmation=require_confirmation,
        )
        toolsets.append(toolset)
        if log_registered:
            logger.info("MCP server '{}' registered (prefix='{}')", server_name, prefix)

    return toolsets


def build_mcp_toolsets_from_env(*, log_registered: bool = True) -> list[ManagedMcpToolset]:
    """Build MCP toolsets from `SENTIENTAGENT_V2_MCP_SERVERS_JSON`."""
    return build_mcp_toolsets(_load_servers_from_env(), log_registered=log_registered)
