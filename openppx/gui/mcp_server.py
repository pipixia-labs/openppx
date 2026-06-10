"""Built-in MCP server exposing desktop GUI tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..runtime.adk_version import assert_supported_adk_major
from ..runtime.client_api_service import ClientApiCoordinator
from .executor import execute_gui_action
from .job_coordinator import (
    gui_task_job_cancel,
    gui_task_job_output,
    gui_task_job_status,
    resume_gui_task_job,
    submit_gui_task_job,
)
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


def submit_gui_task(
    *,
    task: str,
    max_steps: int | None = None,
    dry_run: bool = False,
    planner_model: str | None = None,
    planner_api_key: str | None = None,
    planner_base_url: str | None = None,
) -> dict[str, Any]:
    """Submit a multi-step desktop GUI task as a background job."""
    normalized = (task or "").strip()
    if not normalized:
        return {"ok": False, "error": "task is required"}
    try:
        return submit_gui_task_job(
            task=normalized,
            max_steps=max_steps,
            dry_run=bool(dry_run),
            planner_model=planner_model,
            planner_api_key=planner_api_key,
            planner_base_url=planner_base_url,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_gui_task_status(*, job_id: str) -> dict[str, Any]:
    """Return one background GUI task job status."""
    normalized = (job_id or "").strip()
    if not normalized:
        return {"ok": False, "error": "job_id is required"}
    try:
        return gui_task_job_status(normalized)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_gui_task_output(*, job_id: str) -> dict[str, Any]:
    """Return one background GUI task job output or latest checkpoint."""
    normalized = (job_id or "").strip()
    if not normalized:
        return {"ok": False, "error": "job_id is required"}
    try:
        return gui_task_job_output(normalized)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def cancel_gui_task(
    *,
    job_id: str,
    terminal_status: str = "cancelled",
    reason: str = "",
) -> dict[str, Any]:
    """Request cooperative stop for one background GUI task job."""
    normalized = (job_id or "").strip()
    if not normalized:
        return {"ok": False, "error": "job_id is required"}
    try:
        return gui_task_job_cancel(
            normalized,
            terminal_status=terminal_status,
            reason=reason,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def resume_gui_task(
    *,
    job_id: str = "",
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resume a background GUI task from an explicit or latest job checkpoint."""
    try:
        resume_checkpoint = checkpoint if isinstance(checkpoint, dict) else None
        normalized_job_id = (job_id or "").strip()
        if resume_checkpoint is None:
            if not normalized_job_id:
                return {"ok": False, "error": "job_id or checkpoint is required"}
            status = gui_task_job_status(normalized_job_id)
            if not status.get("ok"):
                return status
            raw_checkpoint = status.get("checkpoint")
            if not isinstance(raw_checkpoint, dict):
                return {"ok": False, "error": "GUI job has no checkpoint"}
            resume_checkpoint = raw_checkpoint
        return resume_gui_task_job(checkpoint=resume_checkpoint)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _coordinator_for_data_dir(data_dir: str | None = None) -> ClientApiCoordinator:
    """Build one local client-api coordinator for MCP access helpers."""
    if data_dir and str(data_dir).strip():
        return ClientApiCoordinator(data_dir=Path(data_dir).expanduser())
    return ClientApiCoordinator()


def get_agent_access(
    *,
    agent_id: str,
    user_id: str = "ppx-client-user",
    data_dir: str | None = None,
) -> dict[str, Any]:
    """Return one agent access snapshot for GUI and MCP workflows."""
    normalized_agent_id = (agent_id or "").strip()
    if not normalized_agent_id:
        return {"ok": False, "error": "agent_id is required"}
    try:
        return _coordinator_for_data_dir(data_dir).get_agent_access(
            normalized_agent_id,
            user_id=user_id,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def list_agent_memory_audit(
    *,
    agent_id: str,
    user_id: str = "ppx-client-user",
    limit: int = 50,
    data_dir: str | None = None,
) -> dict[str, Any]:
    """Return explicit-memory audit rows for GUI and MCP workflows."""
    normalized_agent_id = (agent_id or "").strip()
    if not normalized_agent_id:
        return {"ok": False, "error": "agent_id is required"}
    try:
        return _coordinator_for_data_dir(data_dir).get_memory_audit(
            normalized_agent_id,
            user_id=user_id,
            limit=limit,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def list_agent_access_audit(
    *,
    agent_id: str,
    user_id: str = "ppx-client-user",
    limit: int = 50,
    data_dir: str | None = None,
) -> dict[str, Any]:
    """Return access-mutation audit rows for GUI and MCP workflows."""
    normalized_agent_id = (agent_id or "").strip()
    if not normalized_agent_id:
        return {"ok": False, "error": "agent_id is required"}
    try:
        return _coordinator_for_data_dir(data_dir).get_access_audit(
            normalized_agent_id,
            user_id=user_id,
            limit=limit,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def set_agent_owner(
    *,
    agent_id: str,
    owner_principal_id: str,
    user_id: str = "ppx-client-user",
    data_dir: str | None = None,
) -> dict[str, Any]:
    """Set one agent owner for GUI and MCP workflows."""
    normalized_agent_id = (agent_id or "").strip()
    normalized_owner = (owner_principal_id or "").strip()
    if not normalized_agent_id:
        return {"ok": False, "error": "agent_id is required"}
    if not normalized_owner:
        return {"ok": False, "error": "owner_principal_id is required"}
    try:
        return _coordinator_for_data_dir(data_dir).set_agent_owner(
            normalized_agent_id,
            normalized_owner,
            user_id=user_id,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def add_agent_participant(
    *,
    agent_id: str,
    principal_id: str,
    user_id: str = "ppx-client-user",
    data_dir: str | None = None,
) -> dict[str, Any]:
    """Add or refresh one participant membership for GUI and MCP workflows."""
    normalized_agent_id = (agent_id or "").strip()
    normalized_principal_id = (principal_id or "").strip()
    if not normalized_agent_id:
        return {"ok": False, "error": "agent_id is required"}
    if not normalized_principal_id:
        return {"ok": False, "error": "principal_id is required"}
    try:
        return _coordinator_for_data_dir(data_dir).upsert_agent_membership(
            normalized_agent_id,
            normalized_principal_id,
            relation="participant",
            user_id=user_id,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def remove_agent_participant(
    *,
    agent_id: str,
    principal_id: str,
    user_id: str = "ppx-client-user",
    data_dir: str | None = None,
) -> dict[str, Any]:
    """Remove one participant membership for GUI and MCP workflows."""
    normalized_agent_id = (agent_id or "").strip()
    normalized_principal_id = (principal_id or "").strip()
    if not normalized_agent_id:
        return {"ok": False, "error": "agent_id is required"}
    if not normalized_principal_id:
        return {"ok": False, "error": "principal_id is required"}
    try:
        return _coordinator_for_data_dir(data_dir).delete_agent_membership(
            normalized_agent_id,
            normalized_principal_id,
            user_id=user_id,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def build_gui_mcp_server(name: str = "openppx-gui") -> FastMCP:
    """Build a FastMCP server that exposes GUI automation tools."""
    server = FastMCP(
        name=name,
        instructions=(
            "Desktop GUI automation tools for openppx. Use `gui_action` for one step "
            "and `gui_task` for multi-step workflows. Access helpers can inspect and manage "
            "agent owner/participant relationships."
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

    @server.tool(
        name="gui_task_submit",
        description="Submit a multi-step desktop GUI task as a background job and return job_id.",
    )
    def _gui_task_submit(
        task: str,
        max_steps: int | None = None,
        dry_run: bool = False,
        planner_model: str | None = None,
        planner_api_key: str | None = None,
        planner_base_url: str | None = None,
    ) -> dict[str, Any]:
        return submit_gui_task(
            task=task,
            max_steps=max_steps,
            dry_run=dry_run,
            planner_model=planner_model,
            planner_api_key=planner_api_key,
            planner_base_url=planner_base_url,
        )

    @server.tool(
        name="gui_task_status",
        description="Read one background GUI task job status by job_id.",
    )
    def _gui_task_status(job_id: str) -> dict[str, Any]:
        return get_gui_task_status(job_id=job_id)

    @server.tool(
        name="gui_task_output",
        description="Read one background GUI task job output or latest checkpoint by job_id.",
    )
    def _gui_task_output(job_id: str) -> dict[str, Any]:
        return get_gui_task_output(job_id=job_id)

    @server.tool(
        name="gui_task_cancel",
        description="Request cooperative stop for one background GUI task job.",
    )
    def _gui_task_cancel(
        job_id: str,
        terminal_status: str = "cancelled",
        reason: str = "",
    ) -> dict[str, Any]:
        return cancel_gui_task(
            job_id=job_id,
            terminal_status=terminal_status,
            reason=reason,
        )

    @server.tool(
        name="gui_task_resume",
        description="Resume a background GUI task from an explicit or latest job checkpoint.",
    )
    def _gui_task_resume(
        job_id: str = "",
        checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return resume_gui_task(job_id=job_id, checkpoint=checkpoint)

    @server.tool(
        name="agent_access_get",
        description="Read one agent access snapshot, including visible owner and participant relationships.",
    )
    def _agent_access_get(
        agent_id: str,
        user_id: str = "ppx-client-user",
        data_dir: str | None = None,
    ) -> dict[str, Any]:
        return get_agent_access(agent_id=agent_id, user_id=user_id, data_dir=data_dir)

    @server.tool(
        name="agent_memory_audit_list",
        description="List visible explicit-memory access audit rows for one agent.",
    )
    def _agent_memory_audit_list(
        agent_id: str,
        user_id: str = "ppx-client-user",
        limit: int = 50,
        data_dir: str | None = None,
    ) -> dict[str, Any]:
        return list_agent_memory_audit(
            agent_id=agent_id,
            user_id=user_id,
            limit=limit,
            data_dir=data_dir,
        )

    @server.tool(
        name="agent_access_audit_list",
        description="List visible owner/member access-mutation audit rows for one agent.",
    )
    def _agent_access_audit_list(
        agent_id: str,
        user_id: str = "ppx-client-user",
        limit: int = 50,
        data_dir: str | None = None,
    ) -> dict[str, Any]:
        return list_agent_access_audit(
            agent_id=agent_id,
            user_id=user_id,
            limit=limit,
            data_dir=data_dir,
        )

    @server.tool(
        name="agent_owner_set",
        description="Set one agent owner. This requires a root-level requester.",
    )
    def _agent_owner_set(
        agent_id: str,
        owner_principal_id: str,
        user_id: str = "ppx-client-user",
        data_dir: str | None = None,
    ) -> dict[str, Any]:
        return set_agent_owner(
            agent_id=agent_id,
            owner_principal_id=owner_principal_id,
            user_id=user_id,
            data_dir=data_dir,
        )

    @server.tool(
        name="agent_participant_add",
        description="Add or refresh one participant membership for an agent.",
    )
    def _agent_participant_add(
        agent_id: str,
        principal_id: str,
        user_id: str = "ppx-client-user",
        data_dir: str | None = None,
    ) -> dict[str, Any]:
        return add_agent_participant(
            agent_id=agent_id,
            principal_id=principal_id,
            user_id=user_id,
            data_dir=data_dir,
        )

    @server.tool(
        name="agent_participant_remove",
        description="Remove one participant membership from an agent.",
    )
    def _agent_participant_remove(
        agent_id: str,
        principal_id: str,
        user_id: str = "ppx-client-user",
        data_dir: str | None = None,
    ) -> dict[str, Any]:
        return remove_agent_participant(
            agent_id=agent_id,
            principal_id=principal_id,
            user_id=user_id,
            data_dir=data_dir,
        )

    return server


def main() -> None:
    """Run the built-in GUI MCP server."""
    assert_supported_adk_major()
    server_name = (os.getenv("OPENPPX_GUI_MCP_NAME", "") or "openppx-gui").strip()
    transport = (os.getenv("OPENPPX_GUI_MCP_TRANSPORT", "") or "stdio").strip().lower()
    if transport not in _SUPPORTED_TRANSPORTS:
        allowed = ", ".join(sorted(_SUPPORTED_TRANSPORTS))
        raise ValueError(
            f"Invalid OPENPPX_GUI_MCP_TRANSPORT='{transport}'. Supported values: {allowed}."
        )
    build_gui_mcp_server(name=server_name).run(transport=transport)


if __name__ == "__main__":
    main()
