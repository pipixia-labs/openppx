"""Step-event normalization and ADK plugin integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin

from ..bus.events import OutboundMessage
from .tool_context import get_route

logger = logging.getLogger(__name__)

_STEP_EVENT_PUBLISHER = None


def configure_step_event_publisher(publisher) -> None:
    """Configure the async publisher used by runtime step events."""

    global _STEP_EVENT_PUBLISHER
    _STEP_EVENT_PUBLISHER = publisher


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _legacy_feedback_event_class(feedback_type: str) -> str:
    mapping = {
        "status": "step_update",
        "tool": "step_update",
        "tool_output": "step_output",
    }
    return mapping.get(feedback_type, "final_text")


def _legacy_feedback_phase(status: str, *, done: bool) -> str:
    normalized = status.strip().lower()
    if normalized in {"accepted", "pending"}:
        return "queued"
    if normalized in {"queued", "started", "running", "waiting", "finished", "failed", "cancelled"}:
        return normalized
    if normalized in {"error", "errored"}:
        return "failed"
    if normalized in {"completed", "complete", "done", "success", "succeeded"}:
        return "finished"
    if done:
        return "finished"
    return "running" if normalized else ""


def normalize_outbound_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return a normalized metadata dict for channel consumption."""

    source = metadata if isinstance(metadata, dict) else {}
    normalized = dict(source)
    if normalized.get("_stream_delta"):
        normalized["_event_class"] = "stream_delta"
        return normalized
    if normalized.get("_stream_end"):
        normalized["_event_class"] = "stream_end"
        return normalized

    event_class = _clean_str(normalized.get("_event_class"))
    feedback_type = _clean_str(normalized.get("_feedback_type")).lower()
    feedback_status = _clean_str(normalized.get("_feedback_status"))
    done = _bool(normalized.get("_done"))

    if not event_class and feedback_type:
        event_class = _legacy_feedback_event_class(feedback_type)
        normalized["_event_class"] = event_class

    if event_class in {"step_update", "step_output"}:
        if "_step_phase" not in normalized:
            phase = _legacy_feedback_phase(feedback_status, done=done)
            if phase:
                normalized["_step_phase"] = phase
        if "_step_kind" not in normalized:
            tool_name = _clean_str(normalized.get("_tool_name"))
            task_id = _clean_str(normalized.get("_task_id"))
            origin = _clean_str(normalized.get("_feedback_origin")).lower()
            if task_id and tool_name == "spawn_subagent":
                normalized["_step_kind"] = "subagent"
            elif tool_name:
                normalized["_step_kind"] = "tool"
            elif origin == "runtime":
                normalized["_step_kind"] = "runtime"
            else:
                normalized["_step_kind"] = "system"
        if "_step_title" not in normalized:
            title = _clean_str(normalized.get("_tool_name"))
            if title:
                normalized["_step_title"] = title
        if "_step_id" not in normalized:
            step_id = (
                _clean_str(normalized.get("_function_call_id"))
                or _clean_str(normalized.get("_task_id"))
                or _clean_str(normalized.get("_session_id"))
            )
            if step_id:
                normalized["_step_id"] = step_id
        normalized["_done"] = done
        normalized["_important"] = _bool(normalized.get("_important"))
    elif event_class:
        normalized["_done"] = done
        normalized["_important"] = _bool(normalized.get("_important"))

    return normalized


def build_step_metadata(
    *,
    event_class: str = "step_update",
    step_phase: str,
    step_title: str,
    step_kind: str,
    content: str | None = None,
    invocation_id: str | None = None,
    function_call_id: str | None = None,
    step_id: str | None = None,
    step_order: int | None = None,
    event_seq: int | None = None,
    step_update_kind: str = "status",
    feedback_status: str | None = None,
    tool_name: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    done: bool = False,
    important: bool = False,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one normalized step-event metadata payload."""

    metadata: dict[str, Any] = {
        "_event_class": event_class,
        "_step_phase": step_phase,
        "_step_update_kind": step_update_kind,
        "_step_title": step_title,
        "_step_kind": step_kind,
        "_done": bool(done),
        "_important": bool(important),
        "_feedback_type": "tool_output" if event_class == "step_output" else "status",
        "_feedback_status": feedback_status or step_phase,
    }
    if invocation_id:
        metadata["_invocation_id"] = invocation_id
    if function_call_id:
        metadata["_function_call_id"] = function_call_id
    resolved_step_id = step_id or function_call_id or task_id or session_id
    if resolved_step_id:
        metadata["_step_id"] = resolved_step_id
    if step_order is not None:
        metadata["_step_order"] = step_order
    if event_seq is not None:
        metadata["_event_seq"] = event_seq
    if tool_name:
        metadata["_tool_name"] = tool_name
    if task_id:
        metadata["_task_id"] = task_id
    if session_id:
        metadata["_session_id"] = session_id
    if content:
        metadata["_content_preview"] = content[:200]
    if extra_metadata:
        metadata.update(extra_metadata)
    return normalize_outbound_metadata(metadata)


@dataclass(slots=True)
class NormalizedOutboundEvent:
    """A lightweight normalized view of one outbound message."""

    event_class: str
    content: str
    metadata: dict[str, Any]

    @property
    def is_stream(self) -> bool:
        return self.event_class in {"stream_delta", "stream_end"}


def classify_outbound_message(content: str, metadata: dict[str, Any] | None) -> NormalizedOutboundEvent:
    """Classify one outbound payload for manager/channel handling."""

    normalized = normalize_outbound_metadata(metadata)
    event_class = _clean_str(normalized.get("_event_class")) or "final_text"
    return NormalizedOutboundEvent(event_class=event_class, content=content, metadata=normalized)


class OpenPpxStepEventPlugin(BasePlugin):
    """ADK plugin that emits normalized step events for tool lifecycle updates."""

    def __init__(self) -> None:
        super().__init__(name="openppx_step_events")
        self._event_seq_by_invocation: dict[str, int] = {}
        self._step_order_by_invocation: dict[str, int] = {}
        self._known_steps: dict[str, set[str]] = {}

    async def before_run_callback(self, *, invocation_context: Any) -> None:
        invocation_id = _clean_str(getattr(invocation_context, "invocation_id", None))
        if not invocation_id:
            return None
        self._event_seq_by_invocation[invocation_id] = 0
        self._step_order_by_invocation[invocation_id] = 0
        self._known_steps[invocation_id] = set()
        return None

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        invocation_id = _clean_str(getattr(invocation_context, "invocation_id", None))
        if invocation_id:
            self._event_seq_by_invocation.pop(invocation_id, None)
            self._step_order_by_invocation.pop(invocation_id, None)
            self._known_steps.pop(invocation_id, None)

    def _next_event_seq(self, invocation_id: str) -> int:
        current = self._event_seq_by_invocation.get(invocation_id, 0) + 1
        self._event_seq_by_invocation[invocation_id] = current
        return current

    def _ensure_step_order(self, invocation_id: str, step_id: str) -> int:
        known = self._known_steps.setdefault(invocation_id, set())
        if step_id not in known:
            known.add(step_id)
            self._step_order_by_invocation[invocation_id] = self._step_order_by_invocation.get(invocation_id, 0) + 1
        return self._step_order_by_invocation.get(invocation_id, 1)

    async def _publish_step_event(
        self,
        *,
        invocation_id: str,
        function_call_id: str,
        tool_name: str,
        step_phase: str,
        content: str,
        step_update_kind: str = "status",
        done: bool = False,
        important: bool = False,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        if _STEP_EVENT_PUBLISHER is None:
            return
        channel, chat_id = get_route()
        if not channel or not chat_id:
            return

        step_order = self._ensure_step_order(invocation_id, function_call_id)
        metadata: dict[str, Any] = {
            **build_step_metadata(
                event_class="step_update",
                invocation_id=invocation_id,
                event_seq=self._next_event_seq(invocation_id),
                step_id=function_call_id,
                function_call_id=function_call_id,
                step_phase=step_phase,
                step_update_kind=step_update_kind,
                step_title=tool_name,
                step_kind="tool",
                step_order=step_order,
                tool_name=tool_name,
                done=done,
                important=important,
                content=content,
            ),
            "_feedback_origin": "adk_plugin",
        }
        if extra_metadata:
            metadata.update(extra_metadata)

        try:
            await _STEP_EVENT_PUBLISHER(
                OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=content,
                    metadata=metadata,
                )
            )
        except Exception:
            logger.exception("Failed publishing openppx step event")

    async def before_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        tool_context: Any,
    ) -> None:
        invocation_id = _clean_str(getattr(tool_context, "invocation_id", None))
        function_call_id = _clean_str(getattr(tool_context, "function_call_id", None))
        tool_name = _clean_str(getattr(tool, "name", None)) or "tool"
        if not invocation_id or not function_call_id:
            return None
        await self._publish_step_event(
            invocation_id=invocation_id,
            function_call_id=function_call_id,
            tool_name=tool_name,
            step_phase="started",
            content=f"Started `{tool_name}`",
        )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        tool_context: Any,
        result: Any,
    ) -> None:
        invocation_id = _clean_str(getattr(tool_context, "invocation_id", None))
        function_call_id = _clean_str(getattr(tool_context, "function_call_id", None))
        tool_name = _clean_str(getattr(tool, "name", None)) or "tool"
        if not invocation_id or not function_call_id:
            return None
        await self._publish_step_event(
            invocation_id=invocation_id,
            function_call_id=function_call_id,
            tool_name=tool_name,
            step_phase="finished",
            content=f"Finished `{tool_name}`",
            done=True,
        )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        tool_context: Any,
        error: Exception,
    ) -> None:
        invocation_id = _clean_str(getattr(tool_context, "invocation_id", None))
        function_call_id = _clean_str(getattr(tool_context, "function_call_id", None))
        tool_name = _clean_str(getattr(tool, "name", None)) or "tool"
        if not invocation_id or not function_call_id:
            return None
        await self._publish_step_event(
            invocation_id=invocation_id,
            function_call_id=function_call_id,
            tool_name=tool_name,
            step_phase="failed",
            content=f"`{tool_name}` failed: {type(error).__name__}",
            done=True,
            important=True,
        )
        return None

    async def on_event_callback(self, *, invocation_context: Any, event: Any) -> None:
        invocation_id = _clean_str(getattr(invocation_context, "invocation_id", None))
        if not invocation_id or event is None:
            return None

        long_running_ids = set(getattr(event, "long_running_tool_ids", None) or [])
        get_function_calls = getattr(event, "get_function_calls", None)
        if not callable(get_function_calls):
            return None
        for function_call in get_function_calls() or []:
            function_call_id = _clean_str(getattr(function_call, "id", None))
            tool_name = _clean_str(getattr(function_call, "name", None)) or "tool"
            if not function_call_id or function_call_id not in long_running_ids:
                continue
            await self._publish_step_event(
                invocation_id=invocation_id,
                function_call_id=function_call_id,
                tool_name=tool_name,
                step_phase="waiting",
                content=f"`{tool_name}` is running in the background",
                step_update_kind="status",
            )
        return None
