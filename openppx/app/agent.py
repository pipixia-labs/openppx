"""Google ADK root agent for openppx."""

from __future__ import annotations

import os
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import LongRunningFunctionTool
from google.adk.tools import load_artifacts
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from ..core.config import normalize_agent_privilege_level
from ..core.env_utils import env_enabled
from ..core.mcp_registry import build_mcp_toolsets_from_env
from ..core.provider import build_adk_model_from_env
from ..tooling.skills_adapter import list_skills, read_skill
from ..tooling.registry import (
    browser,
    computer_task,
    computer_use,
    cron,
    edit_file,
    exec_command,
    glob,
    grep,
    list_dir,
    message_file,
    message,
    message_image,
    read_file,
    process_session,
    spawn_subagent,
    web_fetch,
    web_search,
    write_file,
)
from .prompt import (
    build_root_agent_instruction,
    build_startup_runtime_context,
    build_static_policy_instruction,
    gui_builtin_tools_enabled,
)


def _gui_builtin_tools_enabled() -> bool:
    """Return whether legacy builtin GUI tools should be exposed."""
    return gui_builtin_tools_enabled()


def _agent_privilege_level() -> str:
    """Return the current agent privilege level from environment."""
    raw = os.getenv("OPENPPX_AGENT_PRIVILEGE_LEVEL", "").strip().lower()
    if not raw:
        return ""
    return normalize_agent_privilege_level(raw)


def _can_delegate() -> bool:
    """Return whether the current agent may delegate to sub-agents."""
    return env_enabled("OPENPPX_CAN_DELEGATE", default=True)


def _tool_name(tool: Any) -> str:
    """Return a stable tool name for filtering/debug output."""
    if hasattr(tool, "name") and isinstance(getattr(tool, "name"), str):
        return getattr(tool, "name")
    if hasattr(tool, "func"):
        func = getattr(tool, "func")
        return getattr(func, "__name__", str(tool))
    return getattr(tool, "__name__", str(tool))


def _build_instruction() -> str:
    """Build the root-agent instruction from layered prompt sections."""
    return build_root_agent_instruction()


def _build_static_instruction() -> str:
    """Build stable root-agent policy for ADK ``static_instruction``."""
    return build_static_policy_instruction()


def _build_dynamic_instruction() -> str:
    """Build startup/runtime context for ADK dynamic ``instruction``."""
    return build_startup_runtime_context()


def _build_tools() -> list[Any]:
    """Assemble builtin tools plus optional MCP toolsets from env config."""
    base_tools: list[Any] = [
        PreloadMemoryTool(),
        load_artifacts,
        list_skills,
        read_skill,
        read_file,
        write_file,
        edit_file,
        list_dir,
        glob,
        grep,
        exec_command,
        process_session,
        browser,
        web_search,
        web_fetch,
        message,
        message_image,
        message_file,
        cron,
    ]
    if _can_delegate():
        base_tools.append(LongRunningFunctionTool(func=spawn_subagent))
    if _gui_builtin_tools_enabled():
        base_tools.extend([computer_task, computer_use])

    privilege_level = _agent_privilege_level()
    if privilege_level == "low":
        allowed_names = {
            "list_skills",
            "read_skill",
            "read_file",
            "list_dir",
            "glob",
            "grep",
            "load_artifacts",
        }
        tools = [tool for tool in base_tools if _tool_name(tool) in allowed_names or isinstance(tool, PreloadMemoryTool)]
        return tools

    if privilege_level == "medium":
        blocked_names = {"message", "message_image", "message_file"}
        tools = [tool for tool in base_tools if _tool_name(tool) not in blocked_names]
        tools.extend(build_mcp_toolsets_from_env())
        return tools

    tools = list(base_tools)
    tools.extend(build_mcp_toolsets_from_env())
    return tools


root_agent = LlmAgent(
    name="openppx",
    model=build_adk_model_from_env(),
    static_instruction=_build_static_instruction(),
    instruction=_build_dynamic_instruction(),
    tools=_build_tools(),
)
