"""Built-in MCP server exposing desktop GUI tools."""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .executor import execute_gui_action
from .task_runner import execute_gui_task

_SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable-http"}


def run_gui_action(
    *,
    action: str,
    dry_run: bool = False,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Execute one screenshot-grounded desktop GUI action.

    This wrapper is shared by MCP tools and tests. It keeps response shape
    stable by always returning a dict with `ok`.
    """
    normalized = (action or "").strip()
    if not normalized:
        return {"ok": False, "error": "action is required"}
    try:
        return execute_gui_action(
            action=normalized,
            dry_run=bool(dry_run),
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_gui_task(
    *,
    task: str,
    max_steps: int | None = None,
    dry_run: bool = False,
    planner_model: str | None = None,
    planner_api_key: str | None = None,
    planner_base_url: str | None = None,
) -> dict[str, Any]:
    """Run a multi-step desktop GUI task using planner + action execution."""
    normalized = (task or "").strip()
    if not normalized:
        return {"ok": False, "error": "task is required"}
    try:
        return execute_gui_task(
            task=normalized,
            max_steps=max_steps,
            dry_run=bool(dry_run),
            planner_model=planner_model,
            planner_api_key=planner_api_key,
            planner_base_url=planner_base_url,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def build_gui_mcp_server(name: str = "openpipixia-gui") -> FastMCP:
    """Build a FastMCP server that exposes GUI automation tools."""
    server = FastMCP(
        name=name,
        instructions=(
            "Desktop GUI automation tools for openpipixia. Use `gui_action` for one step "
            "and `gui_task` for multi-step workflows."
        ),
    )

    @server.tool(
        name="gui_action",
        description="Execute one desktop GUI action grounded from a screenshot.",
    )
    def _gui_action(
        action: str,
        dry_run: bool = False,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        return run_gui_action(
            action=action,
            dry_run=dry_run,
            model=model,
            api_key=api_key,
            base_url=base_url,
        )

    @server.tool(
        name="gui_task",
        description="Run a multi-step desktop GUI task with planner + executor loop.",
    )
    def _gui_task(
        task: str,
        max_steps: int | None = None,
        dry_run: bool = False,
        planner_model: str | None = None,
        planner_api_key: str | None = None,
        planner_base_url: str | None = None,
    ) -> dict[str, Any]:
        return run_gui_task(
            task=task,
            max_steps=max_steps,
            dry_run=dry_run,
            planner_model=planner_model,
            planner_api_key=planner_api_key,
            planner_base_url=planner_base_url,
        )

    return server


def main() -> None:
    """Run the built-in GUI MCP server."""
    server_name = (os.getenv("OPENPIPIXIA_GUI_MCP_NAME", "") or "openpipixia-gui").strip()
    transport = (os.getenv("OPENPIPIXIA_GUI_MCP_TRANSPORT", "") or "stdio").strip().lower()
    if transport not in _SUPPORTED_TRANSPORTS:
        allowed = ", ".join(sorted(_SUPPORTED_TRANSPORTS))
        raise ValueError(
            f"Invalid OPENPIPIXIA_GUI_MCP_TRANSPORT='{transport}'. Supported values: {allowed}."
        )
    build_gui_mcp_server(name=server_name).run(transport=transport)


if __name__ == "__main__":
    main()
