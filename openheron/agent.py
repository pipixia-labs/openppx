"""Google ADK root agent for openheron."""

from __future__ import annotations

import os
import platform
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import LongRunningFunctionTool
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from .mcp_registry import build_mcp_toolsets_from_env
from .provider import build_adk_model_from_env
from .runtime.debug_callbacks import after_model_debug_callback, before_model_debug_callback
from .runtime.workspace_bootstrap import before_model_workspace_bootstrap_callback
from .skills import get_registry, list_skills, read_skill
from .tools import (
    browser,
    cron,
    edit_file,
    exec_command,
    list_dir,
    message,
    message_image,
    read_file,
    process_session,
    spawn_subagent,
    web_fetch,
    web_search,
    write_file,
)


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


def _build_instruction() -> str:
    runtime = f"{platform.system()} {platform.machine()} / Python"
    workspace = os.getenv("OPENHERON_WORKSPACE", os.getcwd())
    skills_summary = get_registry().build_summary()

    return f"""You are openheron, a lightweight skills-first coding assistant.

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
- Use `spawn_subagent(prompt=...)` for background sub-tasks that should finish later.
- Prefer these built-in tools for actions: `read_file`, `write_file`, `edit_file`, `list_dir`, `exec`, `process`, `browser`, `web_search`, `web_fetch`, `message`, `message_image`, `cron`, `spawn_subagent`.
- Browser routing supports `target=host|node|sandbox`; use `target=node` with `node=<id>` when a specific node proxy is required.
- For long-running shell tasks, use `exec(background=true|yield_ms=...)` and follow-up with `process(...)`.
- Current time is injected into each request payload (e.g. `Current request time`).
  For relative scheduling, always use that injected request time as `now`.

Available skills:
{skills_summary}
"""


def _build_tools() -> list[Any]:
    """Assemble builtin tools plus optional MCP toolsets from env config."""
    tools: list[Any] = [
        PreloadMemoryTool(),
        list_skills,
        read_skill,
        read_file,
        write_file,
        edit_file,
        list_dir,
        exec_command,
        process_session,
        browser,
        web_search,
        web_fetch,
        message,
        message_image,
        cron,
        LongRunningFunctionTool(func=spawn_subagent),
    ]
    tools.extend(build_mcp_toolsets_from_env())
    return tools


root_agent = LlmAgent(
    name="openheron",
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
