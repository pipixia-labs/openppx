"""Gateway that bridges bus/channel traffic to ADK Runner."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from google.genai import types

from ..bus.events import InboundMessage, OutboundMessage
from ..bus.queue import MessageBus
from ..channels.manager import ChannelManager
from ..runtime.adk_utils import extract_text, merge_text_stream
from ..runtime.cron_helpers import cron_store_path
from ..runtime.cron_service import CronJob, CronService
from ..runtime.heartbeat_status_store import write_heartbeat_status_snapshot
from ..runtime.heartbeat_utils import DEFAULT_HEARTBEAT_PROMPT, HEARTBEAT_TOKEN, strip_heartbeat_token
from ..runtime.heartbeat_runner import HeartbeatRunRequest, HeartbeatRunner
from ..runtime.message_time import append_execution_time, inject_request_time
from ..runtime.runner_factory import create_runner
from ..runtime.subagent_agent import build_restricted_subagent
from ..runtime.tool_context import route_context
from ..core.security import load_security_policy
from ..tooling.registry import (
    SubagentSpawnRequest,
    configure_heartbeat_waker,
    configure_outbound_publisher,
    configure_subagent_dispatcher,
)

logger = logging.getLogger(__name__)

_HELP_TEXT = (
    "openpipixia commands:\n"
    "/new - Start a new conversation session\n"
    "/help - Show available commands"
)


async def _cancel_task(task: asyncio.Task[Any] | None) -> None:
    """Cancel and await one background task safely."""
    if task is None:
        return
    await _cancel_tasks([task])


async def _cancel_tasks(
    tasks: list[asyncio.Task[Any]],
    *,
    on_exception: Callable[[asyncio.Task[Any], Exception], None] | None = None,
) -> None:
    """Cancel and drain tasks, optionally reporting non-cancellation failures."""
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if on_exception is not None:
                on_exception(task, exc)


class Gateway:
    """Consumes inbound messages and executes them via ADK Runner."""

    def __init__(
        self,
        *,
        agent: Any,
        app_name: str,
        bus: MessageBus,
        channel_manager: ChannelManager | None = None,
        session_service: Any | None = None,
    ) -> None:
        self.bus = bus
        self.channel_manager = channel_manager
        self.runner, self.session_service = create_runner(
            agent=agent,
            app_name=app_name,
            session_service=session_service,
        )
        self._subagent_agent = build_restricted_subagent(agent)
        self._subagent_runner, _ = create_runner(
            agent=self._subagent_agent,
            app_name=app_name,
            session_service=self.session_service,
        )
        self._inbound_task: asyncio.Task[None] | None = None
        self._cron_service: CronService | None = None
        self._heartbeat_runner: HeartbeatRunner | None = None
        self._subagent_tasks: dict[str, asyncio.Task[None]] = {}
        self._subagent_semaphore = asyncio.Semaphore(self._subagent_max_concurrency())
        # Map logical inbound session keys (channel:chat_id) to active ADK session ids.
        self._session_overrides: dict[str, str] = {}
        self._inflight_user_requests = 0
        self._last_inbound_route: tuple[str, str] | None = None
        self._last_heartbeat_delivery: dict[str, Any] | None = None

    @staticmethod
    def _subagent_max_concurrency() -> int:
        """Read background sub-agent concurrency from env with safe bounds."""
        raw = os.getenv("OPENPIPIXIA_SUBAGENT_MAX_CONCURRENCY", "2").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 2
        return min(max(value, 1), 16)

    def _cron_store_path(self) -> Path:
        workspace = load_security_policy().workspace_root
        return cron_store_path(workspace)

    def _heartbeat_is_busy(self) -> bool:
        """Return whether gateway is currently handling interactive inbound traffic."""
        return self._inflight_user_requests > 0

    def _request_heartbeat_wake(self, reason: str) -> None:
        if self._heartbeat_runner is None:
            return
        self._heartbeat_runner.request_wake(reason=reason, coalesce_ms=0)

    @staticmethod
    def _heartbeat_ack_max_chars() -> int:
        raw = os.getenv("OPENPIPIXIA_HEARTBEAT_ACK_MAX_CHARS", "300").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 300
        return max(0, value)

    @staticmethod
    def _heartbeat_show_ok() -> bool:
        raw = os.getenv("OPENPIPIXIA_HEARTBEAT_SHOW_OK", "0").strip().lower()
        return raw in {"1", "true", "yes", "on", "enabled"}

    @staticmethod
    def _heartbeat_show_alerts() -> bool:
        raw = os.getenv("OPENPIPIXIA_HEARTBEAT_SHOW_ALERTS", "1").strip().lower()
        return raw in {"1", "true", "yes", "on", "enabled"}

    @staticmethod
    def _heartbeat_target_mode() -> str:
        raw = os.getenv("OPENPIPIXIA_HEARTBEAT_TARGET", "last").strip().lower()
        if raw in {"none", "channel", "last"}:
            return raw
        return "last"

    @staticmethod
    def _heartbeat_target_channel() -> str:
        return os.getenv("OPENPIPIXIA_HEARTBEAT_TARGET_CHANNEL", "").strip() or "local"

    @staticmethod
    def _heartbeat_target_chat_id() -> str:
        return os.getenv("OPENPIPIXIA_HEARTBEAT_TARGET_CHAT_ID", "").strip() or "heartbeat"

    def _resolve_heartbeat_target(self) -> tuple[str, str] | None:
        mode = self._heartbeat_target_mode()
        if mode == "none":
            return None
        if mode == "channel":
            return (self._heartbeat_target_channel(), self._heartbeat_target_chat_id())
        if self._last_inbound_route is not None:
            return self._last_inbound_route
        return ("local", "heartbeat")

    @staticmethod
    def _heartbeat_preview(content: str, *, max_chars: int = 120) -> str:
        """Return one-line preview for heartbeat status/debug output."""
        normalized = " ".join(content.split())
        if len(normalized) <= max_chars:
            return normalized
        if max_chars <= 3:
            return normalized[:max(0, max_chars)]
        return f"{normalized[: max_chars - 3]}..."

    def heartbeat_status(self) -> dict[str, Any]:
        """Return heartbeat runtime status for diagnostics and operator tooling."""
        if self._heartbeat_runner is None:
            runner_status: dict[str, Any] = {
                "running": False,
                "enabled": False,
                "interval_ms": None,
                "active_hours_enabled": False,
                "wake_pending": False,
                "wake_reason": None,
                "last_run_at_ms": None,
                "last_status": None,
                "last_reason": None,
                "last_duration_ms": None,
                "last_error": None,
                "recent_reason_sources": [],
                "recent_reason_counts": {},
            }
        else:
            runner_status = dict(self._heartbeat_runner.status())
        runner_status["target_mode"] = self._heartbeat_target_mode()
        runner_status["last_delivery"] = dict(self._last_heartbeat_delivery or {})
        return runner_status

    def _persist_heartbeat_status_snapshot(self) -> None:
        """Write the latest heartbeat status snapshot for CLI observability."""
        workspace = load_security_policy().workspace_root
        try:
            write_heartbeat_status_snapshot(workspace, self.heartbeat_status())
        except Exception:
            logger.exception("Failed persisting heartbeat status snapshot")

    @staticmethod
    def _heartbeat_task_file_candidates(workspace: Path) -> tuple[Path, ...]:
        """Return heartbeat task file candidate paths in priority order."""
        return (workspace / "HEARTBEAT.md", workspace / "heartbeat.md")

    def _heartbeat_task_gate(self, prompt: str) -> tuple[bool, str]:
        """Return whether heartbeat should invoke LLM under current workspace task state.

        Only the default heartbeat prompt is gated by task-file presence/content.
        Custom prompts are treated as explicit operator intent and run normally.
        """
        if (prompt or "").strip() != DEFAULT_HEARTBEAT_PROMPT:
            return True, ""
        workspace = load_security_policy().workspace_root
        candidate_paths = self._heartbeat_task_file_candidates(workspace)
        task_path = next((path for path in candidate_paths if path.exists()), None)
        if task_path is None:
            return False, "task-missing"
        try:
            content = task_path.read_text(encoding="utf-8")
        except Exception:
            # Keep heartbeat runnable when task file cannot be read.
            return True, ""
        if not content.strip():
            return False, "task-empty"
        return True, ""

    async def _run_heartbeat(self, req: HeartbeatRunRequest) -> None:
        """Execute one heartbeat turn through the shared ADK runner."""
        try:
            should_invoke, skip_kind = self._heartbeat_task_gate(req.prompt)
            if not should_invoke:
                self._last_heartbeat_delivery = {
                    "reason": req.reason,
                    "kind": skip_kind,
                    "delivered": False,
                }
                return
            prompt = append_execution_time(req.prompt)
            request = types.UserContent(parts=[types.Part.from_text(text=prompt)])
            final = await self._run_text_stream(
                runner=self.runner,
                channel="local",
                chat_id="heartbeat",
                default_when_empty=None,
                user_id="heartbeat",
                session_id="heartbeat:main",
                new_message=request,
            )
            normalized = strip_heartbeat_token(
                final,
                mode="heartbeat",
                max_ack_chars=self._heartbeat_ack_max_chars(),
            )
            target = self._resolve_heartbeat_target()
            if target is None:
                self._last_heartbeat_delivery = {
                    "reason": req.reason,
                    "kind": "target-none",
                    "delivered": False,
                }
                return
            target_channel, target_chat_id = target
            if normalized.should_skip:
                if self._heartbeat_show_ok():
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=target_channel,
                            chat_id=target_chat_id,
                            content=HEARTBEAT_TOKEN,
                            metadata={"system": "heartbeat", "reason": req.reason},
                        )
                    )
                    self._last_heartbeat_delivery = {
                        "reason": req.reason,
                        "kind": "ok",
                        "delivered": True,
                        "target_channel": target_channel,
                        "target_chat_id": target_chat_id,
                        "content_preview": HEARTBEAT_TOKEN,
                    }
                else:
                    self._last_heartbeat_delivery = {
                        "reason": req.reason,
                        "kind": "ok-muted",
                        "delivered": False,
                        "target_channel": target_channel,
                        "target_chat_id": target_chat_id,
                    }
                return
            if not self._heartbeat_show_alerts():
                self._last_heartbeat_delivery = {
                    "reason": req.reason,
                    "kind": "alert-muted",
                    "delivered": False,
                    "target_channel": target_channel,
                    "target_chat_id": target_chat_id,
                }
                return
            content = normalized.text.strip() or (final or "").strip()
            if not content:
                self._last_heartbeat_delivery = {
                    "reason": req.reason,
                    "kind": "empty",
                    "delivered": False,
                    "target_channel": target_channel,
                    "target_chat_id": target_chat_id,
                }
                return
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=target_channel,
                    chat_id=target_chat_id,
                    content=content,
                    metadata={"system": "heartbeat", "reason": req.reason},
                )
            )
            self._last_heartbeat_delivery = {
                "reason": req.reason,
                "kind": "alert",
                "delivered": True,
                "target_channel": target_channel,
                "target_chat_id": target_chat_id,
                "content_preview": self._heartbeat_preview(content),
            }
        finally:
            self._persist_heartbeat_status_snapshot()

    async def _persist_session_memory_snapshot(self, *, user_id: str, session_id: str) -> None:
        """Persist one session snapshot into configured memory service.

        This is used by explicit session-boundary commands (for example `/new`)
        so users can force a memory flush before switching to a new session id.
        """
        memory_service = getattr(self.runner, "memory_service", None)
        if memory_service is None:
            return

        try:
            session = await self.session_service.get_session(
                app_name=self.runner.app_name,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception:
            logger.exception(
                "Failed to load session before memory snapshot (user_id=%s session_id=%s)",
                user_id,
                session_id,
            )
            return

        if session is None:
            return

        try:
            await memory_service.add_session_to_memory(session)
        except ValueError:
            # Align with root agent callback: memory service may be absent/disabled.
            return
        except Exception:
            logger.exception(
                "Failed to persist session memory snapshot (user_id=%s session_id=%s)",
                user_id,
                session_id,
            )

    async def _run_text_stream(
        self,
        *,
        runner: Any,
        channel: str,
        chat_id: str,
        default_when_empty: str | None = "(no response)",
        **run_kwargs: Any,
    ) -> str:
        """Run one ADK stream and merge emitted text parts into final output."""
        final = ""
        with route_context(channel, chat_id):
            async for event in runner.run_async(**run_kwargs):
                text = extract_text(getattr(event, "content", None))
                final = merge_text_stream(final, text)
        if final:
            return final
        if default_when_empty is None:
            return ""
        return default_when_empty

    async def _run_cron_job(self, job: CronJob) -> str | None:
        """Execute a scheduled cron job through the shared ADK runner."""
        target_channel = job.payload.channel or "local"
        target_chat_id = job.payload.to or "default"
        prompt = append_execution_time(job.payload.message)
        request = types.UserContent(parts=[types.Part.from_text(text=prompt)])
        final = await self._run_text_stream(
            runner=self.runner,
            channel=target_channel,
            chat_id=target_chat_id,
            user_id="cron",
            session_id=f"cron:{job.id}",
            new_message=request,
        )
        if job.payload.deliver:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=target_channel,
                    chat_id=target_chat_id,
                    content=final,
                )
            )
        if self._heartbeat_runner is not None:
            self._heartbeat_runner.request_wake(reason=f"cron:{job.id}", coalesce_ms=0)
        return final

    async def start(self) -> None:
        if self._inbound_task and not self._inbound_task.done():
            return
        # Tools call `message(...)` from inside runner execution; this bridges
        # those tool-level sends back into the outbound queue.
        configure_outbound_publisher(self.bus.publish_outbound)
        configure_subagent_dispatcher(self._dispatch_subagent_request)
        configure_heartbeat_waker(self._request_heartbeat_wake)
        if self._cron_service is None:
            self._cron_service = CronService(self._cron_store_path(), on_job=self._run_cron_job)
        if self._heartbeat_runner is None:
            self._heartbeat_runner = HeartbeatRunner(
                on_run=self._run_heartbeat,
                is_busy=self._heartbeat_is_busy,
            )
        await self._cron_service.start()
        await self._heartbeat_runner.start()
        if self.channel_manager:
            await self.channel_manager.start_all()
            await self.channel_manager.start_dispatcher()
        self._inbound_task = asyncio.create_task(self._consume_inbound())

    async def stop(self) -> None:
        if self._heartbeat_runner is not None:
            await self._heartbeat_runner.stop()
        if self._cron_service is not None:
            self._cron_service.stop()
        configure_heartbeat_waker(None)
        configure_subagent_dispatcher(None)
        configure_outbound_publisher(None)
        await self._stop_subagent_tasks()
        await _cancel_task(self._inbound_task)
        self._inbound_task = None
        if self.channel_manager:
            await self.channel_manager.stop_dispatcher()
            await self.channel_manager.stop_all()

    async def process_message(self, msg: InboundMessage) -> OutboundMessage:
        command = msg.content.strip().lower()
        if command == "/help":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=_HELP_TEXT,
                metadata=msg.metadata,
            )
        if command == "/new":
            active_session_id = self._session_overrides.get(msg.session_key, msg.session_key)
            await self._persist_session_memory_snapshot(
                user_id=msg.sender_id,
                session_id=active_session_id,
            )
            self._session_overrides[msg.session_key] = f"{msg.session_key}:new:{uuid.uuid4().hex[:12]}"
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Started a new conversation session.",
                metadata=msg.metadata,
            )

        active_session_id = self._session_overrides.get(msg.session_key, msg.session_key)
        prompt = inject_request_time(msg.content, received_at=msg.timestamp)
        request = types.UserContent(parts=[types.Part.from_text(text=prompt)])
        # Route context lets tools like `message(...)` infer the current target.
        final = await self._run_text_stream(
            runner=self.runner,
            channel=msg.channel,
            chat_id=msg.chat_id,
            user_id=msg.sender_id,
            session_id=active_session_id,
            new_message=request,
        )
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final,
            metadata=msg.metadata,
        )

    def _dispatch_subagent_request(self, request: SubagentSpawnRequest) -> asyncio.Task[None] | None:
        """Schedule one background sub-agent request onto the current event loop."""
        if request.task_id in self._subagent_tasks:
            return self._subagent_tasks[request.task_id]
        task = asyncio.create_task(
            self._run_subagent_request(request),
            name=f"subagent-{request.task_id}",
        )
        self._subagent_tasks[request.task_id] = task
        task.add_done_callback(lambda _task, task_id=request.task_id: self._subagent_tasks.pop(task_id, None))
        return task

    async def _stop_subagent_tasks(self) -> None:
        if not self._subagent_tasks:
            return
        pending = list(self._subagent_tasks.values())
        await _cancel_tasks(
            pending,
            on_exception=lambda _task, _exc: logger.exception("Background sub-agent task stopped with exception"),
        )
        self._subagent_tasks.clear()

    async def _run_subagent_request(self, request: SubagentSpawnRequest) -> None:
        """Execute a sub-agent task, resume parent invocation, then notify target."""
        async with self._subagent_semaphore:
            response_payload: dict[str, Any]
            try:
                subagent_result = await self._execute_subagent_prompt(request)
                response_payload = {
                    "status": "completed",
                    "task_id": request.task_id,
                    "result": subagent_result,
                }
            except Exception as exc:
                logger.exception(
                    "Sub-agent background execution failed (task_id=%s)", request.task_id
                )
                response_payload = {
                    "status": "error",
                    "task_id": request.task_id,
                    "error": str(exc),
                }

            resume_text = ""
            try:
                resume_text = await self._resume_parent_invocation(request, response_payload)
            except Exception as exc:
                logger.exception(
                    "Failed to resume parent invocation for sub-agent (task_id=%s)", request.task_id
                )
                if response_payload.get("status") != "error":
                    response_payload = {
                        "status": "error",
                        "task_id": request.task_id,
                        "error": f"failed to resume parent invocation: {exc}",
                    }

            if request.notify_on_complete:
                await self._publish_subagent_notification(request, resume_text, response_payload)

    async def _execute_subagent_prompt(self, request: SubagentSpawnRequest) -> str:
        """Run the sub-agent prompt in an isolated session and return final text."""
        sub_session_id = f"subagent:{request.task_id}"
        prompt = append_execution_time(request.prompt)
        new_message = types.UserContent(parts=[types.Part.from_text(text=prompt)])
        return await self._run_text_stream(
            runner=self._subagent_runner,
            channel=request.channel,
            chat_id=request.chat_id,
            user_id=request.user_id,
            session_id=sub_session_id,
            new_message=new_message,
        )

    async def _resume_parent_invocation(
        self,
        request: SubagentSpawnRequest,
        response_payload: dict[str, Any],
    ) -> str:
        """Resume the paused parent invocation with sub-agent function response."""
        function_response = types.FunctionResponse(
            name="spawn_subagent",
            id=request.function_call_id,
            response=response_payload,
        )
        new_message = types.Content(
            role="user",
            parts=[types.Part(function_response=function_response)],
        )
        return await self._run_text_stream(
            runner=self.runner,
            channel=request.channel,
            chat_id=request.chat_id,
            default_when_empty=None,
            user_id=request.user_id,
            session_id=request.session_id,
            invocation_id=request.invocation_id,
            new_message=new_message,
        )

    async def _publish_subagent_notification(
        self,
        request: SubagentSpawnRequest,
        resume_text: str,
        response_payload: dict[str, Any],
    ) -> None:
        """Publish one completion notification for a background sub-agent task."""
        if resume_text:
            content = resume_text
        elif response_payload.get("status") == "completed":
            content = (
                f"Sub-agent task completed (id: {request.task_id}).\n\n"
                f"{response_payload.get('result', '(no response)')}"
            )
        else:
            content = (
                f"Sub-agent task failed (id: {request.task_id}). "
                f"{response_payload.get('error', 'unknown error')}"
            )
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=request.channel,
                chat_id=request.chat_id,
                content=content,
            )
        )

    async def _consume_inbound(self) -> None:
        while True:
            # Single worker keeps message order deterministic for this skeleton.
            msg = await self.bus.consume_inbound()
            self._last_inbound_route = (msg.channel, msg.chat_id)
            self._inflight_user_requests += 1
            try:
                response = await self.process_message(msg)
                await self.bus.publish_outbound(response)
            except Exception:
                logger.exception(
                    "Failed processing inbound message (channel=%s chat_id=%s sender_id=%s)",
                    msg.channel,
                    msg.chat_id,
                    msg.sender_id,
                )
            finally:
                self._inflight_user_requests = max(0, self._inflight_user_requests - 1)
