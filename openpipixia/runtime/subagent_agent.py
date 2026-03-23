"""Helpers for building a restricted background sub-agent."""

from __future__ import annotations

from typing import Any

from google.adk.agents import LlmAgent

_BLOCKED_TOOL_NAMES = {"spawn_subagent"}


def _tool_name(tool: Any) -> str:
    """Resolve a tool name from ADK tool objects or Python callables."""
    name = getattr(tool, "name", None)
    if isinstance(name, str) and name:
        return name
    py_name = getattr(tool, "__name__", None)
    if isinstance(py_name, str) and py_name:
        return py_name
    return str(tool)


def build_restricted_subagent(agent: Any) -> Any:
    """Create a sub-agent copy with blocked tools removed.

    The background sub-agent must not recursively spawn more sub-agents.
    For ADK ``LlmAgent`` instances we return a shallow copy with selected tools
    removed from ``tools``. Non-``LlmAgent`` inputs are returned unchanged.

    Args:
        agent: Root agent instance.

    Returns:
        A restricted agent for background execution.
    """
    if not isinstance(agent, LlmAgent):
        return agent

    filtered_tools = [tool for tool in agent.tools if _tool_name(tool) not in _BLOCKED_TOOL_NAMES]
    if len(filtered_tools) == len(agent.tools):
        return agent

    instruction = agent.instruction
    if isinstance(instruction, str):
        instruction = (
            f"{instruction}\n\n"
            "[Sub-agent runtime note] The `spawn_subagent` tool is disabled in this worker."
        )

    return agent.model_copy(
        deep=False,
        update={
            "tools": filtered_tools,
            "instruction": instruction,
        },
    )

