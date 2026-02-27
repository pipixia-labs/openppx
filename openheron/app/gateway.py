"""Gateway that bridges bus/channel traffic to ADK Runner."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
from ..runtime.route_stats_store import write_route_stats_snapshot
from ..runtime.heartbeat_utils import HEARTBEAT_TOKEN, strip_heartbeat_token
from ..runtime.heartbeat_runner import HeartbeatRunRequest, HeartbeatRunner
from ..runtime.message_time import append_execution_time, inject_request_time
from ..runtime.agent_routing import AgentRouter, RoutedAgentRequest
from ..runtime.agent_runtime import AgentRuntimeContext, agent_runtime_context, get_current_agent_runtime
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
    "openheron commands:\n"
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
        config: dict[str, Any] | None = None,
    ) -> None:
        self.bus = bus
        self.channel_manager = channel_manager
        self._router = AgentRouter(config)
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
        self._heartbeat_runners: dict[str, HeartbeatRunner] = {}
        # Backward compatibility for tests/legacy callers that access single-runner field.
        self._heartbeat_runner: HeartbeatRunner | None = None
        self._subagent_tasks: dict[str, asyncio.Task[None]] = {}
        self._subagent_semaphore = asyncio.Semaphore(self._subagent_max_concurrency())
        # Map logical inbound session keys (channel:chat_id) to active ADK session ids.
        self._session_overrides: dict[str, str] = {}
        self._route_stats: dict[str, Any] = {
            "totalMessages": 0,
            "byAgent": {},
            "byChannel": {},
            "byMatchedBy": {},
            "recent": [],
        }
        self._route_stats_by_agent: dict[str, dict[str, Any]] = {}
        self._inflight_user_requests = 0
        self._last_inbound_route: tuple[str, str] | None = None
        self._last_heartbeat_delivery_by_agent: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _subagent_max_concurrency() -> int:
        """Read background sub-agent concurrency from env with safe bounds."""
        raw = os.getenv("OPENHERON_SUBAGENT_MAX_CONCURRENCY", "2").strip()
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
        runtime = get_current_agent_runtime()
        if runtime is None:
            default_agent_id = self._router.default_agent_id()
            runner = self._heartbeat_runners.get(default_agent_id)
            if runner is None:
                runner = self._heartbeat_runner
            if runner is not None:
                runner.request_wake(reason=reason, coalesce_ms=0)
            return
        runner = self._heartbeat_runners.get(runtime.agent_id)
        if runner is None:
            return
        runner.request_wake(reason=reason, coalesce_ms=0)

    @staticmethod
    def _heartbeat_setting(
        agent_runtime: AgentRuntimeContext,
        *,
        key: str,
        env_key: str,
        default: object,
    ) -> object:
        raw_env = os.getenv(env_key)
        if raw_env is not None and str(raw_env).strip():
            return raw_env
        if key in agent_runtime.heartbeat:
            return agent_runtime.heartbeat.get(key, default)
        return default

    @classmethod
    def _heartbeat_ack_max_chars(cls, agent_runtime: AgentRuntimeContext) -> int:
        raw = str(
            cls._heartbeat_setting(
                agent_runtime,
                key="ackMaxChars",
                env_key="OPENHERON_HEARTBEAT_ACK_MAX_CHARS",
                default="300",
            )
        ).strip()
        try:
            value = int(raw)
        except ValueError:
            value = 300
        return max(0, value)

    @staticmethod
    def _as_bool(value: object, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if not text:
            return default
        return text in {"1", "true", "yes", "on", "enabled"}

    def _heartbeat_show_ok(self, agent_runtime: AgentRuntimeContext) -> bool:
        value = self._heartbeat_setting(
            agent_runtime,
            key="showOk",
            env_key="OPENHERON_HEARTBEAT_SHOW_OK",
            default=False,
        )
        return self._as_bool(value, default=False)

    def _heartbeat_show_alerts(self, agent_runtime: AgentRuntimeContext) -> bool:
        value = self._heartbeat_setting(
            agent_runtime,
            key="showAlerts",
            env_key="OPENHERON_HEARTBEAT_SHOW_ALERTS",
            default=True,
        )
        return self._as_bool(value, default=True)

    @classmethod
    def _heartbeat_target_mode(cls, agent_runtime: AgentRuntimeContext) -> str:
        raw = str(
            cls._heartbeat_setting(
                agent_runtime,
                key="target",
                env_key="OPENHERON_HEARTBEAT_TARGET",
                default="last",
            )
        ).strip().lower()
        if raw in {"none", "channel", "last"}:
            return raw
        return "last"

    @classmethod
    def _heartbeat_target_channel(cls, agent_runtime: AgentRuntimeContext) -> str:
        return (
            str(
                cls._heartbeat_setting(
                    agent_runtime,
                    key="targetChannel",
                    env_key="OPENHERON_HEARTBEAT_TARGET_CHANNEL",
                    default="",
                )
            ).strip()
            or "local"
        )

    @classmethod
    def _heartbeat_target_chat_id(cls, agent_runtime: AgentRuntimeContext) -> str:
        return (
            str(
                cls._heartbeat_setting(
                    agent_runtime,
                    key="targetChatId",
                    env_key="OPENHERON_HEARTBEAT_TARGET_CHAT_ID",
                    default="",
                )
            ).strip()
            or "heartbeat"
        )

    def _resolve_heartbeat_target(self, *, agent_runtime: AgentRuntimeContext) -> tuple[str, str] | None:
        mode = self._heartbeat_target_mode(agent_runtime)
        if mode == "none":
            return None
        if mode == "channel":
            return (
                self._heartbeat_target_channel(agent_runtime),
                self._heartbeat_target_chat_id(agent_runtime),
            )
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

    @staticmethod
    def _default_heartbeat_status() -> dict[str, Any]:
        return {
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

    def heartbeat_status(self, agent_id: str | None = None) -> dict[str, Any]:
        """Return heartbeat runtime status for diagnostics and operator tooling."""
        if agent_id is None:
            default_agent = self._router.default_agent_id()
            if len(self._heartbeat_runners) == 1:
                only_agent = next(iter(self._heartbeat_runners))
                default_agent = only_agent
            return self.heartbeat_status(default_agent)

        runner = self._heartbeat_runners.get(agent_id)
        runner_status = dict(runner.status()) if runner is not None else self._default_heartbeat_status()
        runtime = self._router.runtime_for_agent(agent_id)
        runner_status["agent_id"] = runtime.agent_id
        runner_status["target_mode"] = self._heartbeat_target_mode(runtime)
        runner_status["last_delivery"] = dict(self._last_heartbeat_delivery_by_agent.get(runtime.agent_id, {}))
        return runner_status

    def _persist_heartbeat_status_snapshot(self, *, agent_runtime: AgentRuntimeContext) -> None:
        """Write one agent heartbeat status snapshot for CLI observability."""
        try:
            payload = self.heartbeat_status(agent_runtime.agent_id)
            write_heartbeat_status_snapshot(agent_runtime.agent_dir, payload)
        except Exception:
            logger.exception("Failed persisting heartbeat status snapshot")

    async def _run_heartbeat(
        self,
        req: HeartbeatRunRequest,
        *,
        agent_runtime: AgentRuntimeContext | None = None,
    ) -> None:
        """Execute one heartbeat turn through the shared ADK runner."""
        effective_runtime = agent_runtime or self._router.runtime_for_agent(None)
        try:
            prompt = append_execution_time(req.prompt)
            request = types.UserContent(parts=[types.Part.from_text(text=prompt)])
            final = await self._run_text_stream(
                runner=self.runner,
                channel="local",
                chat_id="heartbeat",
                agent_runtime=effective_runtime,
                default_when_empty=None,
                user_id="heartbeat",
                session_id=f"heartbeat:{effective_runtime.agent_id}",
                new_message=request,
            )
            normalized = strip_heartbeat_token(
                final,
                mode="heartbeat",
                max_ack_chars=self._heartbeat_ack_max_chars(effective_runtime),
            )
            target = self._resolve_heartbeat_target(agent_runtime=effective_runtime)
            if target is None:
                self._last_heartbeat_delivery_by_agent[effective_runtime.agent_id] = {
                    "reason": req.reason,
                    "kind": "target-none",
                    "delivered": False,
                }
                return
            target_channel, target_chat_id = target
            if normalized.should_skip:
                if self._heartbeat_show_ok(effective_runtime):
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=target_channel,
                            chat_id=target_chat_id,
                            content=HEARTBEAT_TOKEN,
                            metadata={"system": "heartbeat", "reason": req.reason},
                        )
                    )
                    self._last_heartbeat_delivery_by_agent[effective_runtime.agent_id] = {
                        "reason": req.reason,
                        "kind": "ok",
                        "delivered": True,
                        "target_channel": target_channel,
                        "target_chat_id": target_chat_id,
                        "content_preview": HEARTBEAT_TOKEN,
                    }
                else:
                    self._last_heartbeat_delivery_by_agent[effective_runtime.agent_id] = {
                        "reason": req.reason,
                        "kind": "ok-muted",
                        "delivered": False,
                        "target_channel": target_channel,
                        "target_chat_id": target_chat_id,
                    }
                return
            if not self._heartbeat_show_alerts(effective_runtime):
                self._last_heartbeat_delivery_by_agent[effective_runtime.agent_id] = {
                    "reason": req.reason,
                    "kind": "alert-muted",
                    "delivered": False,
                    "target_channel": target_channel,
                    "target_chat_id": target_chat_id,
                }
                return
            content = normalized.text.strip() or (final or "").strip()
            if not content:
                self._last_heartbeat_delivery_by_agent[effective_runtime.agent_id] = {
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
            self._last_heartbeat_delivery_by_agent[effective_runtime.agent_id] = {
                "reason": req.reason,
                "kind": "alert",
                "delivered": True,
                "target_channel": target_channel,
                "target_chat_id": target_chat_id,
                "content_preview": self._heartbeat_preview(content),
            }
        finally:
            self._persist_heartbeat_status_snapshot(agent_runtime=effective_runtime)

    async def _persist_session_memory_snapshot(
        self,
        *,
        user_id: str,
        session_id: str,
        agent_runtime: AgentRuntimeContext | None = None,
    ) -> None:
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
            if agent_runtime is None:
                await memory_service.add_session_to_memory(session)
            else:
                with agent_runtime_context(agent_runtime):
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
        agent_runtime: AgentRuntimeContext | None = None,
        default_when_empty: str | None = "(no response)",
        **run_kwargs: Any,
    ) -> str:
        """Run one ADK stream and merge emitted text parts into final output."""
        final = ""
        if agent_runtime is None:
            with route_context(channel, chat_id):
                async for event in runner.run_async(**run_kwargs):
                    text = extract_text(getattr(event, "content", None))
                    final = merge_text_stream(final, text)
        else:
            with route_context(channel, chat_id), agent_runtime_context(agent_runtime):
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
        default_runner = self._heartbeat_runners.get(self._router.default_agent_id())
        if default_runner is None:
            default_runner = self._heartbeat_runner
        if default_runner is not None:
            default_runner.request_wake(reason=f"cron:{job.id}", coalesce_ms=0)
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
        if not self._heartbeat_runners:
            for runtime in self._router.all_agent_runtimes().values():
                heartbeat_cfg = runtime.heartbeat if isinstance(runtime.heartbeat, dict) else {}
                active_hours_cfg = heartbeat_cfg.get("activeHours")
                active_hours = active_hours_cfg if isinstance(active_hours_cfg, dict) else {}
                self._heartbeat_runners[runtime.agent_id] = HeartbeatRunner(
                    on_run=lambda req, rt=runtime: self._run_heartbeat(req, agent_runtime=rt),
                    every=str(heartbeat_cfg.get("every", "30m")).strip() or "30m",
                    prompt=str(heartbeat_cfg.get("prompt", "")).strip(),
                    active_hours={
                        "start": str(active_hours.get("start", "")).strip(),
                        "end": str(active_hours.get("end", "")).strip(),
                        "timezone": str(active_hours.get("timezone", "user")).strip() or "user",
                    },
                    is_busy=self._heartbeat_is_busy,
                )
        self._heartbeat_runner = self._heartbeat_runners.get(self._router.default_agent_id())
        await self._cron_service.start()
        for runner in self._heartbeat_runners.values():
            await runner.start()
        if self.channel_manager:
            await self.channel_manager.start_all()
            await self.channel_manager.start_dispatcher()
        self._inbound_task = asyncio.create_task(self._consume_inbound())

    async def stop(self) -> None:
        for runner in self._heartbeat_runners.values():
            await runner.stop()
        if self._cron_service is not None:
            self._cron_service.stop()
        configure_heartbeat_waker(None)
        configure_subagent_dispatcher(None)
        configure_outbound_publisher(None)
        await self._stop_subagent_tasks()
        await _cancel_task(self._inbound_task)
        self._inbound_task = None
        self._heartbeat_runners = {}
        self._heartbeat_runner = None
        if self.channel_manager:
            await self.channel_manager.stop_dispatcher()
            await self.channel_manager.stop_all()

    async def process_message(self, msg: InboundMessage) -> OutboundMessage:
        routed: RoutedAgentRequest = self._router.resolve(msg)
        self._record_route_hit(channel=msg.channel, routed=routed)
        command = msg.content.strip().lower()
        if command == "/help":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=_HELP_TEXT,
                metadata=self._attach_route_metadata(msg.metadata, routed),
            )
        if command == "/new":
            active_session_id = self._session_overrides.get(routed.session_base_key, routed.session_base_key)
            await self._persist_session_memory_snapshot(
                user_id=routed.scoped_user_id,
                session_id=active_session_id,
                agent_runtime=routed.runtime,
            )
            self._session_overrides[routed.session_base_key] = (
                f"{routed.session_base_key}:new:{uuid.uuid4().hex[:12]}"
            )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Started a new conversation session.",
                metadata=self._attach_route_metadata(msg.metadata, routed),
            )

        active_session_id = self._session_overrides.get(routed.session_base_key, routed.session_base_key)
        prompt = inject_request_time(msg.content, received_at=msg.timestamp)
        request = types.UserContent(parts=[types.Part.from_text(text=prompt)])
        # Route context lets tools like `message(...)` infer the current target.
        final = await self._run_text_stream(
            runner=self.runner,
            channel=msg.channel,
            chat_id=msg.chat_id,
            agent_runtime=routed.runtime,
            user_id=routed.scoped_user_id,
            session_id=active_session_id,
            new_message=request,
        )
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final,
            metadata=self._attach_route_metadata(msg.metadata, routed),
        )

    def _record_route_hit(self, *, channel: str, routed: RoutedAgentRequest) -> None:
        """Record one route hit for lightweight runtime observability."""
        stats = self._route_stats
        stats["totalMessages"] = int(stats.get("totalMessages", 0)) + 1
        by_agent = stats.setdefault("byAgent", {})
        by_channel = stats.setdefault("byChannel", {})
        by_matched = stats.setdefault("byMatchedBy", {})
        by_agent[routed.agent_id] = int(by_agent.get(routed.agent_id, 0)) + 1
        normalized_channel = str(channel).strip().lower() or "unknown"
        by_channel[normalized_channel] = int(by_channel.get(normalized_channel, 0)) + 1
        by_matched[routed.matched_by] = int(by_matched.get(routed.matched_by, 0)) + 1

        recent = stats.setdefault("recent", [])
        if not isinstance(recent, list):
            recent = []
            stats["recent"] = recent
        recent.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "agentId": routed.agent_id,
                "matchedBy": routed.matched_by,
                "channel": normalized_channel,
                "accountId": routed.account_id,
                "peerKind": routed.peer_kind,
                "peerId": routed.peer_id,
                "guildId": routed.guild_id,
                "teamId": routed.team_id,
                "roles": list(routed.roles),
                "sessionId": routed.session_id,
            }
        )
        max_recent = 200
        if len(recent) > max_recent:
            del recent[:-max_recent]
        agent_stats = self._route_stats_by_agent.setdefault(
            routed.agent_id,
            {
                "totalMessages": 0,
                "byAgent": {},
                "byChannel": {},
                "byMatchedBy": {},
                "recent": [],
            },
        )
        agent_stats["totalMessages"] = int(agent_stats.get("totalMessages", 0)) + 1
        agent_by_agent = agent_stats.setdefault("byAgent", {})
        agent_by_channel = agent_stats.setdefault("byChannel", {})
        agent_by_matched = agent_stats.setdefault("byMatchedBy", {})
        agent_by_agent[routed.agent_id] = int(agent_by_agent.get(routed.agent_id, 0)) + 1
        agent_by_channel[normalized_channel] = int(agent_by_channel.get(normalized_channel, 0)) + 1
        agent_by_matched[routed.matched_by] = int(agent_by_matched.get(routed.matched_by, 0)) + 1
        agent_recent = agent_stats.setdefault("recent", [])
        if not isinstance(agent_recent, list):
            agent_recent = []
            agent_stats["recent"] = agent_recent
        agent_recent.append(recent[-1])
        if len(agent_recent) > max_recent:
            del agent_recent[:-max_recent]
        self._persist_route_stats_snapshot(agent_runtime=routed.runtime)

    def _persist_route_stats_snapshot(self, *, agent_runtime: AgentRuntimeContext) -> None:
        """Write one agent route stats snapshot for CLI observability."""
        agent_payload = self._route_stats_by_agent.get(agent_runtime.agent_id, {})
        payload = {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "agentId": agent_runtime.agent_id,
            **agent_payload,
        }
        try:
            write_route_stats_snapshot(agent_runtime.agent_dir, payload)
        except Exception:
            logger.exception("Failed persisting route stats snapshot")

    @staticmethod
    def _attach_route_metadata(
        metadata: dict[str, Any] | None,
        routed: RoutedAgentRequest,
    ) -> dict[str, Any]:
        """Attach normalized routing details for runtime observability."""
        base = dict(metadata) if isinstance(metadata, dict) else {}
        base["openheron_route"] = {
            "agentId": routed.agent_id,
            "matchedBy": routed.matched_by,
            "accountId": routed.account_id,
            "peerKind": routed.peer_kind,
            "peerId": routed.peer_id,
            "guildId": routed.guild_id,
            "teamId": routed.team_id,
            "roles": list(routed.roles),
            "sessionId": routed.session_id,
        }
        return base

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
