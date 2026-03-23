"""GUI MCP detection and tool-name resolution helpers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .env_utils import is_enabled

_MCP_SERVERS_ENV = "OPENPIPIXIA_MCP_SERVERS_JSON"
_DEFAULT_GUI_SERVER_NAME = "openpipixia_gui"


@dataclass(frozen=True)
class GuiMcpRouting:
    """Resolved GUI MCP routing details."""

    server_name: str
    tool_prefix: str
    action_tool_name: str
    task_tool_name: str


def _pick(raw: dict[str, Any], snake: str, camel: str, default: Any = None) -> Any:
    if snake in raw:
        return raw[snake]
    if camel in raw:
        return raw[camel]
    return default


def _load_mcp_servers_from_env() -> dict[str, Any]:
    """Read configured MCP servers from environment JSON."""
    raw = os.getenv(_MCP_SERVERS_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _resolve_tool_prefix(server_name: str, raw_cfg: dict[str, Any]) -> str:
    prefix = str(_pick(raw_cfg, "tool_name_prefix", "toolNamePrefix", "") or "").strip()
    return prefix or f"mcp_{server_name}_"


def _looks_like_gui_server(server_name: str, raw_cfg: dict[str, Any]) -> bool:
    name = str(server_name).strip().lower()
    if name == _DEFAULT_GUI_SERVER_NAME:
        return True

    command = str(raw_cfg.get("command", "")).strip().lower()
    args_raw = raw_cfg.get("args", [])
    args = [str(item).strip().lower() for item in args_raw] if isinstance(args_raw, list) else []
    command_tokens = " ".join([command, *args]).strip()
    if "openpipixia-gui-mcp" in command_tokens:
        return True
    if "openpipixia.gui.mcp_server" in command_tokens:
        return True

    tool_filter_raw = _pick(raw_cfg, "tool_filter", "toolFilter", [])
    tool_filter = [str(item).strip().lower() for item in tool_filter_raw] if isinstance(tool_filter_raw, list) else []
    return "gui_action" in tool_filter or "gui_task" in tool_filter


def _resolve_gui_mcp_from_servers(mcp_servers: dict[str, Any]) -> GuiMcpRouting | None:
    candidates: list[GuiMcpRouting] = []
    for server_name, raw_cfg in mcp_servers.items():
        if not isinstance(raw_cfg, dict):
            continue
        if not is_enabled(raw_cfg.get("enabled"), default=True):
            continue
        name = str(server_name).strip()
        if not name:
            continue
        if not _looks_like_gui_server(name, raw_cfg):
            continue
        prefix = _resolve_tool_prefix(name, raw_cfg)
        candidates.append(
            GuiMcpRouting(
                server_name=name,
                tool_prefix=prefix,
                action_tool_name=f"{prefix}gui_action",
                task_tool_name=f"{prefix}gui_task",
            )
        )

    if not candidates:
        return None
    for candidate in candidates:
        if candidate.server_name.lower() == _DEFAULT_GUI_SERVER_NAME:
            return candidate
    return candidates[0]


def resolve_gui_mcp_from_env() -> GuiMcpRouting | None:
    """Resolve GUI MCP routing from configured MCP servers env."""
    return _resolve_gui_mcp_from_servers(_load_mcp_servers_from_env())


def resolve_gui_mcp_from_summaries(summaries: list[dict[str, str]]) -> GuiMcpRouting | None:
    """Best-effort GUI MCP routing fallback from toolset summaries."""
    if not summaries:
        return None

    candidates: list[GuiMcpRouting] = []
    for item in summaries:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        prefix = str(item.get("prefix", "")).strip() or f"mcp_{name}_"
        lowered_name = name.lower()
        lowered_prefix = prefix.lower()
        if lowered_name == _DEFAULT_GUI_SERVER_NAME or lowered_prefix.startswith("mcp_gui_"):
            candidates.append(
                GuiMcpRouting(
                    server_name=name,
                    tool_prefix=prefix,
                    action_tool_name=f"{prefix}gui_action",
                    task_tool_name=f"{prefix}gui_task",
                )
            )
    if not candidates:
        return None
    for candidate in candidates:
        if candidate.server_name.lower() == _DEFAULT_GUI_SERVER_NAME:
            return candidate
    return candidates[0]


__all__ = [
    "GuiMcpRouting",
    "resolve_gui_mcp_from_env",
    "resolve_gui_mcp_from_summaries",
]
