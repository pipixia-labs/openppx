"""Minimal long-task context injection for the root agent."""

from __future__ import annotations

from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from .context_engine import LongTaskContextStore
from .task_store import TASK_TERMINAL_STATUSES, TaskCheckpointStore, TaskStore
from .workspace_bootstrap import _insert_before_latest_user_batch

_INJECTED_HEADER = "# Long Task Runtime Context"


def _agent_name(callback_context: Any) -> str:
    name = getattr(callback_context, "agent_name", None)
    return name if isinstance(name, str) else ""


def _session_id(callback_context: Any) -> str:
    session = getattr(callback_context, "session", None)
    value = getattr(session, "id", None)
    return value if isinstance(value, str) else ""


def _content_text(content: Any) -> str:
    parts = getattr(content, "parts", None) or []
    return "\n".join(str(getattr(part, "text", "")) for part in parts if getattr(part, "text", None))


def _already_injected(contents: Any) -> bool:
    return isinstance(contents, list) and any(_INJECTED_HEADER in _content_text(content) for content in contents)


def render_long_task_context(
    *,
    session_id: str,
    task_store: TaskStore | None = None,
    checkpoint_store: TaskCheckpointStore | None = None,
    context_store: LongTaskContextStore | None = None,
    limit: int = 5,
) -> str:
    """Render the concise long-task behavior and active-task context block."""
    store = task_store or TaskStore()
    checkpoints = checkpoint_store or TaskCheckpointStore(db_path=store.db_path)
    context = context_store or LongTaskContextStore(db_path=store.db_path)
    active_statuses = [
        "queued",
        "running",
        "paused",
        "waiting_user",
        "waiting_approval",
        "interrupted",
        "stale",
    ]
    tasks = [
        task
        for task in store.list_tasks(session_id=session_id or None, statuses=active_statuses, limit=limit)
        if task.status not in TASK_TERMINAL_STATUSES
    ]
    lines = [
        _INJECTED_HEADER,
        "",
        "Behavior rules:",
        "- Treat stop/pause as interrupt by default, not cancellation.",
        "- Use task controls from show_task/list_tasks before choosing task actions.",
        "- Use pause_task only when a task advertises durable pause/checkpoint support.",
        "- Call cancel_task only when the user clearly abandons the task.",
        "- When the user says continue, inspect current tasks before starting duplicate work.",
        "- Use resume_task only after checking task status; it may report that the runner cannot resume.",
        "- If a task is waiting for user input, route relevant free text through send_task_input.",
        "- Recorded task input does not prove the runner has consumed it.",
        "- A returned task_id means background execution started; it does not prove the user goal is complete.",
        "- Large task output may be stored as artifacts; inspect artifact metadata instead of copying full logs.",
        "- Goal mirrors and todos are short-term context facts; update them when the user goal or current plan changes.",
        "- Do not treat todos as durable execution state; TaskRun facts remain the source of truth for running work.",
        "- TaskFlow facts describe multi-step DAG plans and current steps; they advance bookkeeping but do not execute external work by themselves.",
        "- Staged summaries are compact context facts, not proof that work completed.",
    ]
    goal = context.get_active_goal(session_id)
    if goal is not None:
        lines.extend(["", "Current goal mirror:"])
        lines.append(f"- goal_id: {goal.goal_id}")
        lines.append(f"- objective: {_truncate(goal.objective, 320)}")
        if goal.completion_criteria:
            lines.append(f"- completion_criteria: {_truncate(goal.completion_criteria, 320)}")
        if goal.current_summary:
            lines.append(f"- current_summary: {_truncate(goal.current_summary, 320)}")
        todos = context.list_todos(session_id=session_id, goal_id=goal.goal_id, limit=10)
    else:
        lines.extend(["", "Current goal mirror: none"])
        todos = context.list_todos(session_id=session_id, limit=10) if session_id else []
    if todos:
        lines.extend(["", "Current todos:"])
        for item in todos:
            lines.append(f"- [{item.status}] {item.content}")
    else:
        lines.extend(["", "Current todos: none"])
    flow = context.get_active_flow(session_id)
    if flow is not None:
        flow_steps = context.list_flow_steps(flow_id=flow.flow_id, limit=10)
        current_step = next((step for step in flow_steps if step.step_id == flow.current_step_id), None)
        lines.extend(["", "Current TaskFlow:"])
        lines.append(f"- flow_id: {flow.flow_id}")
        lines.append(f"- status: {flow.status}")
        lines.append(f"- goal: {_truncate(flow.goal, 320)}")
        if current_step is not None:
            lines.append(f"- current_step: [{current_step.status}] {current_step.title}")
            if current_step.task_id:
                lines.append(f"- current_step_task_id: {current_step.task_id}")
        if flow.blocked_task_id:
            lines.append(f"- blocked_task_id: {flow.blocked_task_id}")
        if flow_steps:
            projection = context.project_flow(flow_id=flow.flow_id)
            if projection.get("ready_step_ids"):
                lines.append(f"- ready_step_ids: {', '.join(projection['ready_step_ids'][:5])}")
            if projection.get("blocked_step_ids"):
                lines.append(f"- blocked_step_ids: {', '.join(projection['blocked_step_ids'][:5])}")
            lines.append("- steps:")
            for step in flow_steps:
                task_suffix = f" task_id={step.task_id}" if step.task_id else ""
                dep_suffix = f" depends_on={','.join(step.depends_on)}" if step.depends_on else ""
                lines.append(f"  - {step.order_index}. [{step.status}] {step.title}{task_suffix}{dep_suffix}")
    else:
        lines.extend(["", "Current TaskFlow: none"])
    summaries = context.list_summaries(session_id=session_id, limit=3) if session_id else []
    if summaries:
        lines.extend(["", "Recent staged summaries:"])
        remaining_budget = 1_200
        for summary in summaries:
            title = summary.title or summary.scope
            content = _truncate(summary.content, min(remaining_budget, 420))
            lines.append(f"- {summary.summary_id} [{summary.source_kind}] {title}: {content}")
            remaining_budget -= len(content)
            if remaining_budget <= 120:
                break
    else:
        lines.extend(["", "Recent staged summaries: none"])
    if tasks:
        lines.extend(["", "Current non-terminal tasks:"])
        for task in tasks:
            summary = task.progress_summary or task.last_error or task.terminal_summary
            if len(summary) > 240:
                summary = summary[:237] + "..."
            lines.append(f"- {task.task_id} [{task.status}] {task.title}: {summary or 'no progress yet'}")
            if task.checkpoint_ref:
                checkpoint = checkpoints.get_checkpoint(task.checkpoint_ref)
                checkpoint_summary = checkpoint.summary if checkpoint is not None else ""
                checkpoint_suffix = f" summary={_truncate(checkpoint_summary, 160)}" if checkpoint_summary else ""
                lines.append(
                    f"  checkpoint_ref={task.checkpoint_ref} resume_policy={task.resume_policy or 'not_resumable'}"
                    f"{checkpoint_suffix}"
                )
    else:
        lines.extend(["", "Current non-terminal tasks: none"])
    return "\n".join(lines)


class LongTaskContextPlugin(BasePlugin):
    """Inject minimal long-task behavior context before model calls."""

    def __init__(
        self,
        *,
        target_agent_name: str | None = None,
        task_store: TaskStore | None = None,
        context_store: LongTaskContextStore | None = None,
    ) -> None:
        super().__init__(name="openppx_long_task_context")
        self._target_agent_name = target_agent_name
        self._task_store = task_store
        self._context_store = context_store

    def _matches_agent(self, callback_context: Any) -> bool:
        if not self._target_agent_name:
            return True
        agent_name = _agent_name(callback_context)
        return not agent_name or agent_name == self._target_agent_name

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> None:
        """Insert long-task context into the non-cacheable latest user batch."""
        if not self._matches_agent(callback_context):
            return None
        contents = getattr(llm_request, "contents", None)
        if _already_injected(contents):
            return None
        text = render_long_task_context(
            session_id=_session_id(callback_context),
            task_store=self._task_store,
            context_store=self._context_store,
        )
        if not isinstance(contents, list):
            contents = []
            llm_request.contents = contents
        injected = types.Content(role="user", parts=[types.Part.from_text(text=text)])
        _insert_before_latest_user_batch(contents, injected)
        return None


def _truncate(text: str, limit: int) -> str:
    """Return a compact single-line context value."""
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."
