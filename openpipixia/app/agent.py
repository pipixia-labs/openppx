"""Google ADK root agent for openpipixia."""

from __future__ import annotations

import os
import platform
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import LongRunningFunctionTool
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from ..core.config import normalize_agent_privilege_level
from ..core.env_utils import env_enabled
from ..core.gui_mcp import resolve_gui_mcp_from_env
from ..core.mcp_registry import build_mcp_toolsets_from_env
from ..core.provider import build_adk_model_from_env
from ..runtime.debug_callbacks import after_model_debug_callback, before_model_debug_callback
from ..runtime.workspace_bootstrap import before_model_workspace_bootstrap_callback
from ..tooling.skills_adapter import get_registry, list_skills, read_skill
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

_GUI_BUILTIN_TOOLS_ENABLED_ENV = "OPENPPX_GUI_BUILTIN_TOOLS_ENABLED"


async def _after_agent_memory_callback(callback_context: Any) -> None:
    """Persist session history into ADK memory when memory service is configured.

    This callback is intentionally tolerant: if memory service is not configured,
    ADK raises ``ValueError`` and we skip persistence so the main interaction
    path is never blocked.
    """
    try:
        await callback_context.add_session_to_memory()
    except ValueError:
        return


def _gui_builtin_tools_enabled() -> bool:
    """Return whether legacy builtin GUI tools should be exposed."""
    return env_enabled(_GUI_BUILTIN_TOOLS_ENABLED_ENV, default=True)


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
    runtime = f"{platform.system()} {platform.machine()} / Python"
    workspace = os.getenv("OPENPPX_WORKSPACE", os.getcwd())
    skills_summary = get_registry().build_summary()
    gui_builtin_enabled = _gui_builtin_tools_enabled()
    gui_mcp_routing = resolve_gui_mcp_from_env()
    mcp_task_tool = gui_mcp_routing.task_tool_name if gui_mcp_routing else "mcp_*_gui_task"
    mcp_action_tool = gui_mcp_routing.action_tool_name if gui_mcp_routing else "mcp_*_gui_action"

    gui_tool_guidance = (
        f"- For desktop GUI tasks, prefer MCP GUI tools when available (`{mcp_task_tool}`, `{mcp_action_tool}`).\n"
        "- Tool selection guidance:\n"
        "  - Prefer `browser(...)` for web tasks that are feasible with browser runtime.\n"
        f"  - Prefer `{mcp_task_tool}(...)` for end-to-end desktop GUI workflows.\n"
        f"  - Use `{mcp_action_tool}(...)` only for single-step GUI actions or debugging one step.\n"
    )
    if gui_builtin_enabled:
        gui_tool_guidance += (
            "- Fallback (legacy builtin): use `computer_task(task=..., max_steps=...)` when MCP GUI tools are unavailable.\n"
            "- Use `computer_use(action=...)` only for single-step builtin GUI actions.\n"
        )

    return f"""You are openpipixia, a lightweight skills-first coding assistant.

Runtime: {runtime}
Workspace: {workspace}

Your job:
1. Solve user tasks directly.
2. Use local skills when relevant.
3. Keep responses concise and actionable.

Rules:
- Channel delivery (e.g. local/Feishu) is handled by the gateway runtime.
- Skill loading is file-based (workspace + built-in SKILL.md).
- Before using a skill deeply, call `list_skills` then `read_skill(name)` for the specific skill.
- Do not invent skill content. Always read SKILL.md first.
- Use `message_image(path=..., caption=...)` when a local image file should be delivered to the current channel.
- Use `message_file(path=..., caption=...)` when a local file should be delivered to the current channel.
- Use `spawn_subagent(prompt=...)` for background sub-tasks that should finish later.
- Prefer these built-in tools for actions: `read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`, `exec`, `process`, `browser`, `web_search`, `web_fetch`, `message`, `message_image`, `message_file`, `cron`, `spawn_subagent`.
{gui_tool_guidance}- Browser routing supports `target=host|node|sandbox`; use `target=node` with `node=<id>` when a specific node proxy is required.
- For long-running shell tasks, use `exec(background=true|yield_ms=...)` and follow-up with `process(...)`.
- Current time is injected into each request payload (e.g. `Current request time`).
  For relative scheduling, always use that injected request time as `now`.

Available skills:
{skills_summary}
"""


def _build_tools() -> list[Any]:
    """Assemble builtin tools plus optional MCP toolsets from env config."""
    base_tools: list[Any] = [
        PreloadMemoryTool(),
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
    name="openpipixia",
    model=build_adk_model_from_env(),
    instruction=_build_instruction(),
    after_agent_callback=_after_agent_memory_callback,
    before_model_callback=[
        before_model_workspace_bootstrap_callback,
        before_model_debug_callback,
    ],
    after_model_callback=after_model_debug_callback,
    tools=_build_tools(),
)
