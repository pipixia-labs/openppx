"""Gateway that bridges bus/channel traffic to ADK Runner."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from google.genai import types

from .bus.events import InboundMessage, OutboundMessage
from .bus.queue import MessageBus
from .channels.manager import ChannelManager
from .runtime.adk_utils import extract_text, merge_text_stream
from .runtime.cron_service import CronJob, CronService
from .runtime.message_time import append_execution_time, inject_request_time
from .runtime.runner_factory import create_runner
from .runtime.subagent_agent import build_restricted_subagent
from .runtime.tool_context import route_context
from .security import load_security_policy
from .tools import SubagentSpawnRequest, configure_outbound_publisher, configure_subagent_dispatcher

logger = logging.getLogger(__name__)


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
        self._subagent_tasks: dict[str, asyncio.Task[None]] = {}
        self._subagent_semaphore = asyncio.Semaphore(self._subagent_max_concurrency())

    @staticmethod
    def _subagent_max_concurrency() -> int:
        """Read background sub-agent concurrency from env with safe bounds."""
        raw = os.getenv("SENTIENTAGENT_V2_SUBAGENT_MAX_CONCURRENCY", "2").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 2
        return min(max(value, 1), 16)

    def _cron_store_path(self) -> Path:
        workspace = load_security_policy().workspace_root
        return workspace / ".sentientagent_v2" / "cron_jobs.json"

    async def _run_cron_job(self, job: CronJob) -> str | None:
        """Execute a scheduled cron job through the shared ADK runner."""
        target_channel = job.payload.channel or "local"
        target_chat_id = job.payload.to or "default"
        prompt = append_execution_time(job.payload.message)
        request = types.UserContent(parts=[types.Part.from_text(text=prompt)])
        final = ""
        with route_context(target_channel, target_chat_id):
            async for event in self.runner.run_async(
                user_id="cron",
                session_id=f"cron:{job.id}",
                new_message=request,
            ):
                text = extract_text(getattr(event, "content", None))
                final = merge_text_stream(final, text)
        if not final:
            final = "(no response)"
        if job.payload.deliver:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=target_channel,
                    chat_id=target_chat_id,
                    content=final,
                )
            )
        return final

    async def start(self) -> None:
        if self._inbound_task and not self._inbound_task.done():
            return
        # Tools call `message(...)` from inside runner execution; this bridges
        # those tool-level sends back into the outbound queue.
        configure_outbound_publisher(self.bus.publish_outbound)
        configure_subagent_dispatcher(self._dispatch_subagent_request)
        if self._cron_service is None:
            self._cron_service = CronService(self._cron_store_path(), on_job=self._run_cron_job)
        await self._cron_service.start()
        if self.channel_manager:
            await self.channel_manager.start_all()
            await self.channel_manager.start_dispatcher()
        self._inbound_task = asyncio.create_task(self._consume_inbound())

    async def stop(self) -> None:
        if self._cron_service is not None:
            self._cron_service.stop()
        configure_subagent_dispatcher(None)
        configure_outbound_publisher(None)
        await self._stop_subagent_tasks()
        if self._inbound_task:
            self._inbound_task.cancel()
            try:
                await self._inbound_task
            except asyncio.CancelledError:
                pass
            self._inbound_task = None
        if self.channel_manager:
            await self.channel_manager.stop_dispatcher()
            await self.channel_manager.stop_all()

    async def process_message(self, msg: InboundMessage) -> OutboundMessage:
        prompt = inject_request_time(msg.content, received_at=msg.timestamp)
        request = types.UserContent(parts=[types.Part.from_text(text=prompt)])
        final = ""
        # Route context lets tools like `message(...)` infer the current target.
        with route_context(msg.channel, msg.chat_id):
            async for event in self.runner.run_async(
                user_id=msg.sender_id,
                session_id=msg.session_key,
                new_message=request,
            ):
                text = extract_text(getattr(event, "content", None))
                final = merge_text_stream(final, text)
        if not final:
            final = "(no response)"
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
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Background sub-agent task stopped with exception")
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
        final = ""
        with route_context(request.channel, request.chat_id):
            async for event in self._subagent_runner.run_async(
                user_id=request.user_id,
                session_id=sub_session_id,
                new_message=new_message,
            ):
                text = extract_text(getattr(event, "content", None))
                final = merge_text_stream(final, text)
        return final or "(no response)"

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
        final = ""
        with route_context(request.channel, request.chat_id):
            async for event in self.runner.run_async(
                user_id=request.user_id,
                session_id=request.session_id,
                invocation_id=request.invocation_id,
                new_message=new_message,
            ):
                text = extract_text(getattr(event, "content", None))
                final = merge_text_stream(final, text)
        return final

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
