"""Prompt construction helpers for the openppx root agent."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass

from ..core.env_utils import env_enabled
from ..core.gui_mcp import resolve_gui_mcp_from_env
from ..tooling.skills_adapter import get_registry

GUI_BUILTIN_TOOLS_ENABLED_ENV = "OPENPPX_GUI_BUILTIN_TOOLS_ENABLED"


@dataclass(frozen=True, slots=True)
class RootPromptLayers:
    """Rendered prompt layers for the root openppx agent."""

    static_policy: str
    startup_context: str

    def render(self) -> str:
        """Return the complete root-agent instruction."""
        parts = [self.static_policy.strip(), self.startup_context.strip()]
        return "\n\n".join(part for part in parts if part)


def gui_builtin_tools_enabled() -> bool:
    """Return whether legacy builtin GUI tools should be exposed."""
    return env_enabled(GUI_BUILTIN_TOOLS_ENABLED_ENV, default=True)


def build_static_policy_instruction() -> str:
    """Build the stable root-agent policy instruction.

    This layer intentionally excludes workspace paths, skill summaries, MCP
    routing, and per-request values so it can later become the cacheable prompt
    prefix if ADK context caching is enabled.
    """
    return """You are openppx, a lightweight skills-first coding assistant.

Your job:
1. Solve user tasks directly.
2. Use local skills when relevant.
3. Keep responses concise and actionable.

Rules:
- Channel delivery (e.g. local/Feishu) is handled by the gateway runtime.
- Agent-home context injected at runtime may provide project-specific instructions; follow those more specific instructions when they do not conflict with safety or tool constraints.
- Skill loading is file-based. Before using a skill deeply, call `list_skills` then `read_skill(name)` for the specific skill.
- Do not invent skill content. Always read SKILL.md first.
- Use `message_image(path=..., caption=...)` when a local image file should be delivered to the current channel.
- Use `message_file(path=..., caption=...)` when a local file should be delivered to the current channel.
- Use `spawn_subagent(prompt=...)` for background sub-tasks that should finish later.
- Prefer available built-in tools for file, shell, browser, web, messaging, cron, and sub-agent actions.
- Browser routing supports `target=host|node|sandbox`; use `target=node` with `node=<id>` when a specific node proxy is required.
- For long-running shell tasks, use `exec(background=true|yield_ms=...)` and follow-up with `process(...)`.
- For relative scheduling, use the per-request time injected with the user message as `now`.
"""


def _build_gui_tool_guidance() -> str:
    """Build startup-time GUI tool routing guidance."""
    gui_mcp_routing = resolve_gui_mcp_from_env()
    mcp_task_tool = gui_mcp_routing.task_tool_name if gui_mcp_routing else "mcp_*_gui_task"
    mcp_action_tool = gui_mcp_routing.action_tool_name if gui_mcp_routing else "mcp_*_gui_action"

    guidance = (
        f"- For desktop GUI tasks, prefer MCP GUI tools when available (`{mcp_task_tool}`, `{mcp_action_tool}`).\n"
        "- Tool selection guidance:\n"
        "  - Prefer `browser(...)` for web tasks that are feasible with browser runtime.\n"
        f"  - Prefer `{mcp_task_tool}(...)` for end-to-end desktop GUI workflows.\n"
        f"  - Use `{mcp_action_tool}(...)` only for single-step GUI actions or debugging one step.\n"
    )
    if gui_builtin_tools_enabled():
        guidance += (
            "- Fallback (legacy builtin): use `computer_task(task=..., max_steps=...)` when MCP GUI tools are unavailable.\n"
            "- Use `computer_use(action=...)` only for single-step builtin GUI actions.\n"
        )
    return guidance.rstrip()


def build_startup_runtime_context() -> str:
    """Build startup-time context that should not be treated as stable policy."""
    runtime = f"{platform.system()} {platform.machine()} / Python"
    workspace = os.getenv("OPENPPX_WORKSPACE", os.getcwd())
    skills_summary = get_registry().build_summary()

    return f"""# Runtime Context

This block is startup context, not a user task. Use it silently when answering
the actual user request; do not acknowledge, summarize, or respond to this
block by itself.

Runtime: {runtime}
Workspace: {workspace}

# Tool Routing

{_build_gui_tool_guidance()}

Available skills:

{skills_summary}
"""


def build_root_prompt_layers() -> RootPromptLayers:
    """Build the root prompt layers for openppx."""
    return RootPromptLayers(
        static_policy=build_static_policy_instruction(),
        startup_context=build_startup_runtime_context(),
    )


def build_root_agent_instruction() -> str:
    """Build the complete root-agent instruction from explicit prompt layers."""
    return build_root_prompt_layers().render()
