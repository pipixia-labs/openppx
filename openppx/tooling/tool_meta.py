"""Static metadata for built-in openppx tools."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolMeta:
    """Runtime-neutral metadata for scheduling and policy decisions."""

    read_only: bool
    exclusive: bool = False
    concurrency_safe: bool = True
    category: str = "general"
    risk: str = "low"


TOOL_META: dict[str, ToolMeta] = {
    "load_artifacts": ToolMeta(read_only=True, category="memory"),
    "list_skills": ToolMeta(read_only=True, category="skill"),
    "read_skill": ToolMeta(read_only=True, category="skill"),
    "list_skill_api_runners": ToolMeta(read_only=True, category="skill"),
    "invoke_skill_api": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="task", risk="high"),
    "long_task": ToolMeta(read_only=False, exclusive=False, concurrency_safe=False, category="task", risk="low"),
    "write_todos": ToolMeta(read_only=False, exclusive=False, concurrency_safe=False, category="task", risk="low"),
    "complete_goal": ToolMeta(read_only=False, exclusive=False, concurrency_safe=False, category="task", risk="low"),
    "write_task_flow": ToolMeta(read_only=False, exclusive=False, concurrency_safe=False, category="task", risk="low"),
    "show_task_flow": ToolMeta(read_only=True, category="task"),
    "list_task_flows": ToolMeta(read_only=True, category="task"),
    "update_task_flow_step": ToolMeta(read_only=False, exclusive=False, concurrency_safe=False, category="task", risk="low"),
    "advance_task_flow": ToolMeta(read_only=False, exclusive=False, concurrency_safe=False, category="task", risk="low"),
    "finish_task_flow": ToolMeta(read_only=False, exclusive=False, concurrency_safe=False, category="task", risk="low"),
    "write_context_summary": ToolMeta(read_only=False, exclusive=False, concurrency_safe=False, category="task", risk="low"),
    "summarize_context_text": ToolMeta(read_only=False, exclusive=False, concurrency_safe=False, category="task", risk="low"),
    "evaluate_staged_summary_quality_cases": ToolMeta(read_only=True, category="task"),
    "summarize_staged_summary_quality_log": ToolMeta(read_only=True, category="task"),
    "list_context_summaries": ToolMeta(read_only=True, category="task"),
    "rollup_context_summaries": ToolMeta(read_only=False, exclusive=False, concurrency_safe=False, category="task", risk="low"),
    "list_tasks": ToolMeta(read_only=True, category="task"),
    "show_task": ToolMeta(read_only=True, category="task"),
    "task_control_snapshot": ToolMeta(read_only=True, category="task"),
    "task_output": ToolMeta(read_only=True, category="task"),
    "task_runtime_status": ToolMeta(read_only=True, category="task"),
    "audit_stuck_tasks": ToolMeta(read_only=True, category="task"),
    "remediate_stuck_tasks": ToolMeta(
        read_only=False,
        exclusive=True,
        concurrency_safe=False,
        category="task",
        risk="high",
    ),
    "audit_orphan_runtime_facts": ToolMeta(read_only=True, category="task"),
    "audit_checkpoint_retention": ToolMeta(read_only=True, category="task"),
    "cleanup_terminal_tasks": ToolMeta(
        read_only=False,
        exclusive=True,
        concurrency_safe=False,
        category="task",
        risk="high",
    ),
    "cleanup_orphan_runtime_facts": ToolMeta(
        read_only=False,
        exclusive=True,
        concurrency_safe=False,
        category="task",
        risk="high",
    ),
    "cleanup_checkpoint_retention": ToolMeta(
        read_only=False,
        exclusive=True,
        concurrency_safe=False,
        category="task",
        risk="high",
    ),
    "restart_task": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="task", risk="high"),
    "dispatch_task_action": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="task", risk="high"),
    "resume_task": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="task", risk="medium"),
    "pause_task": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="task", risk="medium"),
    "send_task_input": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="task", risk="medium"),
    "interrupt_task": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="task", risk="high"),
    "cancel_task": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="task", risk="high"),
    "read_file": ToolMeta(read_only=True, category="filesystem"),
    "list_dir": ToolMeta(read_only=True, category="filesystem"),
    "glob": ToolMeta(read_only=True, category="filesystem"),
    "grep": ToolMeta(read_only=True, category="filesystem"),
    "web_search": ToolMeta(read_only=True, category="web"),
    "web_fetch": ToolMeta(read_only=True, category="web"),
    "write_file": ToolMeta(read_only=False, category="filesystem", risk="medium"),
    "edit_file": ToolMeta(read_only=False, category="filesystem", risk="medium"),
    "message": ToolMeta(read_only=False, category="communication", risk="high"),
    "message_image": ToolMeta(read_only=False, category="communication", risk="high"),
    "message_file": ToolMeta(read_only=False, category="communication", risk="high"),
    "cron": ToolMeta(read_only=False, category="automation", risk="high"),
    "spawn_subagent": ToolMeta(read_only=False, category="delegation", risk="high"),
    "exec": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="process", risk="high"),
    "process": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="process", risk="high"),
    "browser": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="browser", risk="high"),
    "list_browser_remote_jobs": ToolMeta(read_only=True, category="browser"),
    "list_browser_remote_providers": ToolMeta(read_only=True, category="browser"),
    "check_browser_remote_job_protocol": ToolMeta(
        read_only=False,
        exclusive=True,
        concurrency_safe=False,
        category="browser",
        risk="high",
    ),
    "start_gui_task": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="gui", risk="high"),
    "computer_task": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="gui", risk="high"),
    "computer_use": ToolMeta(read_only=False, exclusive=True, concurrency_safe=False, category="gui", risk="high"),
}


def get_tool_meta(name: str) -> ToolMeta | None:
    """Return metadata for a built-in tool by public tool name."""

    return TOOL_META.get(name)
