"""Gateway that bridges bus/channel traffic to ADK Runner."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from google.genai import types

from ..bus.events import InboundMessage, OutboundMessage
from ..bus.queue import MessageBus
from ..channels.manager import ChannelManager
from ..runtime.access_policy import AccessPolicy
from ..runtime.adk_utils import extract_text, merge_text_stream
from ..runtime.agent_access_runtime import ensure_agent_access_record
from ..runtime.agent_access_store import AgentAccessStore, AgentMembership, create_agent_access_store
from ..runtime.cron_helpers import cron_store_path
from ..runtime.cron_service import CronJob, CronService
from ..runtime.heartbeat_status_store import write_heartbeat_status_snapshot
from ..runtime.heartbeat_utils import DEFAULT_HEARTBEAT_PROMPT, HEARTBEAT_TOKEN, strip_heartbeat_token
from ..runtime.heartbeat_runner import HeartbeatRunRequest, HeartbeatRunner
from ..runtime.identity_models import ResolvedPrincipal
from ..runtime.identity_store import create_identity_store
from ..runtime.interaction_context import InteractionContext
from ..runtime.message_time import append_execution_time, inject_request_time
from ..runtime.run_config import build_run_config
from ..runtime.runner_factory import create_runner
from ..runtime.step_events import build_step_metadata
from ..runtime.step_events import configure_step_event_publisher
from ..runtime.subagent_agent import build_restricted_subagent
from ..runtime.tool_context import route_context
from ..core.config import get_agent_home_dir
from ..core.security import load_security_policy
from ..tooling.registry import (
    SubagentSpawnRequest,
    configure_heartbeat_waker,
    configure_outbound_publisher,
    configure_subagent_dispatcher,
)

logger = logging.getLogger(__name__)

_HELP_TEXT = (
    "openppx commands:\n"
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
        identity_store: Any | None = None,
        agent_access_store: AgentAccessStore | None = None,
    ) -> None:
        self.app_name = app_name
        self._agent_id = getattr(agent, "name", app_name) or app_name
        self.bus = bus
        self.channel_manager = channel_manager
        self._identity_store = identity_store or create_identity_store()
        self._agent_access_store = agent_access_store or create_agent_access_store()
        self._access_policy = AccessPolicy(
            identity_store=self._identity_store,
            agent_access_store=self._agent_access_store,
        )
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
        # Map logical inbound session keys to active ADK session ids.
        self._session_overrides: dict[str, str] = {}
        self._inflight_user_requests = 0
        self._last_inbound_route: tuple[str, str] | None = None
        self._last_heartbeat_delivery: dict[str, Any] | None = None
        self._ensure_agent_record()

    @staticmethod
    def _subagent_max_concurrency() -> int:
        """Read background sub-agent concurrency from env with safe bounds."""
        raw = os.getenv("OPENPPX_SUBAGENT_MAX_CONCURRENCY", "2").strip()
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
        raw = os.getenv("OPENPPX_HEARTBEAT_ACK_MAX_CHARS", "300").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 300
        return max(0, value)

    @staticmethod
    def _heartbeat_show_ok() -> bool:
        raw = os.getenv("OPENPPX_HEARTBEAT_SHOW_OK", "0").strip().lower()
        return raw in {"1", "true", "yes", "on", "enabled"}

    @staticmethod
    def _heartbeat_show_alerts() -> bool:
        raw = os.getenv("OPENPPX_HEARTBEAT_SHOW_ALERTS", "1").strip().lower()
        return raw in {"1", "true", "yes", "on", "enabled"}

    @staticmethod
    def _heartbeat_target_mode() -> str:
        raw = os.getenv("OPENPPX_HEARTBEAT_TARGET", "last").strip().lower()
        if raw in {"none", "channel", "last"}:
            return raw
        return "last"

    @staticmethod
    def _heartbeat_target_channel() -> str:
        return os.getenv("OPENPPX_HEARTBEAT_TARGET_CHANNEL", "").strip() or "local"

    @staticmethod
    def _heartbeat_target_chat_id() -> str:
        return os.getenv("OPENPPX_HEARTBEAT_TARGET_CHAT_ID", "").strip() or "heartbeat"

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

    def _session_route_key(self, *, channel: str, chat_id: str, principal_id: str) -> str:
        """Return the logical route key for one agent/chat/principal session."""
        return f"{self._agent_id}:{channel}:{chat_id}:{principal_id}"

    def _resolve_message_principal(self, msg: InboundMessage) -> ResolvedPrincipal:
        """Resolve one inbound sender into the principal used by ADK runtime."""
        return self._identity_store.resolve_message_principal(
            channel=msg.channel,
            sender_id=msg.sender_id,
        )

    def _resolve_service_principal(self, name: str) -> ResolvedPrincipal:
        """Resolve one internal runtime principal."""
        return self._identity_store.resolve_service_principal(name)

    def _ensure_agent_record(self) -> None:
        """Ensure the current agent has a baseline access record."""
        ensure_agent_access_record(
            agent_id=self._agent_id,
            agent_name=self._agent_id,
            identity_store=self._identity_store,
            agent_access_store=self._agent_access_store,
            apply_env_overrides=True,
        )

    def _ensure_principal_membership(self, principal: ResolvedPrincipal) -> str:
        """Ensure runtime access rows exist for the current principal and return its relation."""
        self._ensure_agent_record()
        if principal.principal_type == "human":
            existing = self._agent_access_store.get_membership(
                agent_id=self._agent_id,
                principal_id=principal.principal_id,
            )
            if existing is None:
                self._agent_access_store.upsert_membership(
                    AgentMembership(
                        agent_id=self._agent_id,
                        principal_id=principal.principal_id,
                        relation="participant",
                        metadata={"auto_registered": True, **dict(principal.metadata)},
                    )
                )
        return self._access_policy.relation_to_agent(
            requester_principal_id=principal.principal_id,
            agent_id=self._agent_id,
        )

    def _build_interaction_context(
        self,
        *,
        principal: ResolvedPrincipal,
        channel: str,
        chat_id: str,
        session_id: str,
        session_route_key: str,
    ) -> InteractionContext:
        """Build the invocation-scoped context injected into ADK `temp:` state."""
        relation_to_agent = self._ensure_principal_membership(principal)
        return InteractionContext(
            app_name=self.app_name,
            agent_id=self._agent_id,
            session_id=session_id,
            session_route_key=session_route_key,
            channel=channel,
            chat_id=chat_id,
            requester_principal_id=principal.principal_id,
            requester_principal_type=principal.principal_type,
            requester_level=principal.privilege_level,
            requester_relation_to_agent=relation_to_agent,
            requester_account_kind=principal.account_kind,
            authenticated=principal.authenticated,
            requester_display_name=principal.display_name,
            external_subject_id=principal.external_subject_id or "",
            external_display_id=principal.external_display_id or "",
            memory_ingest_enabled=principal.memory_ingest_enabled,
            metadata=dict(principal.metadata),
        )

    async def _current_event_count(self, *, runner: Any, user_id: str, session_id: str) -> int:
        """Return the number of persisted events before the next invocation starts."""
        get_session = getattr(self.session_service, "get_session", None)
        if not callable(get_session):
            return 0
        try:
            session = await get_session(
                app_name=getattr(runner, "app_name", self.app_name),
                user_id=user_id,
                session_id=session_id,
            )
        except Exception:
            logger.exception(
                "Failed to inspect session before run (user_id=%s session_id=%s)",
                user_id,
                session_id,
            )
            return 0
        if session is None:
            return 0
        return len(getattr(session, "events", []) or [])

    async def _build_state_delta(self, *, runner: Any, interaction_context: InteractionContext) -> dict[str, Any]:
        """Build per-invocation `state_delta` for runtime context and ingest offset."""
        ingest_offset = await self._current_event_count(
            runner=runner,
            user_id=interaction_context.requester_principal_id,
            session_id=interaction_context.session_id,
        )
        return interaction_context.to_state_delta(ingest_offset=ingest_offset)

    @staticmethod
    def _guess_mime_type(path: Path) -> str:
        """Return one MIME type for a local media file path."""
        mime_type, _ = mimetypes.guess_type(path.name)
        return mime_type or "application/octet-stream"

    def _build_media_part(self, media_path: str) -> types.Part | None:
        """Convert one local media file into an inline ADK part."""
        path = Path(media_path).expanduser()
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            logger.warning("Skipping missing media attachment: %s", media_path)
            return None
        except Exception:
            logger.exception("Failed reading media attachment: %s", media_path)
            return None
        return types.Part(
            inline_data=types.Blob(
                data=data,
                mime_type=self._guess_mime_type(path),
                display_name=path.name,
            )
        )

    def _build_user_request(
        self,
        *,
        text: str,
        media_paths: list[str],
        received_at: Any,
    ) -> types.UserContent:
        """Build one ADK user content payload from text plus local media files."""
        parts: list[types.Part] = []
        if text.strip():
            prompt = inject_request_time(text, received_at=received_at)
            parts.append(types.Part.from_text(text=prompt))
        for media_path in media_paths:
            media_part = self._build_media_part(media_path)
            if media_part is not None:
                parts.append(media_part)
        if not parts:
            prompt = inject_request_time(text, received_at=received_at)
            parts.append(types.Part.from_text(text=prompt))
        return types.UserContent(parts=parts)

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
        agent_home = get_agent_home_dir()
        return (agent_home / "HEARTBEAT.md", agent_home / "heartbeat.md")

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
        heartbeat_step_id = f"heartbeat:{req.reason}"
        try:
            should_invoke, skip_kind = self._heartbeat_task_gate(req.prompt)
            if not should_invoke:
                self._last_heartbeat_delivery = {
                    "reason": req.reason,
                    "kind": skip_kind,
                    "delivered": False,
                }
                return
            principal = self._resolve_service_principal("heartbeat")
            session_route_key = self._session_route_key(
                channel="local",
                chat_id="heartbeat",
                principal_id=principal.principal_id,
            )
            interaction_context = self._build_interaction_context(
                principal=principal,
                channel="local",
                chat_id="heartbeat",
                session_id="heartbeat:main",
                session_route_key=session_route_key,
            )
            state_delta = await self._build_state_delta(
                runner=self.runner,
                interaction_context=interaction_context,
            )
            prompt = append_execution_time(req.prompt)
            request = types.UserContent(parts=[types.Part.from_text(text=prompt)])
            final = await self._run_text_stream(
                runner=self.runner,
                channel="local",
                chat_id="heartbeat",
                default_when_empty=None,
                user_id=principal.principal_id,
                session_id="heartbeat:main",
                new_message=request,
                state_delta=state_delta,
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
                            metadata={
                                **build_step_metadata(
                                    step_phase="finished",
                                    step_update_kind="result",
                                    step_title="Heartbeat OK",
                                    step_kind="runtime",
                                    step_id=heartbeat_step_id,
                                    session_id="heartbeat:main",
                                    tool_name="heartbeat",
                                    done=True,
                                    content=HEARTBEAT_TOKEN,
                                ),
                                "_feedback_origin": "runtime",
                                "system": "heartbeat",
                                "reason": req.reason,
                            },
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
                    metadata={
                        **build_step_metadata(
                            step_phase="finished",
                            step_update_kind="result",
                            step_title="Heartbeat alert",
                            step_kind="runtime",
                            step_id=heartbeat_step_id,
                            session_id="heartbeat:main",
                            tool_name="heartbeat",
                            done=True,
                            content=content,
                        ),
                        "_feedback_origin": "runtime",
                        "system": "heartbeat",
                        "reason": req.reason,
                    },
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
        emit_stream: bool = False,
        **run_kwargs: Any,
    ) -> str:
        """Run one ADK stream and merge emitted text parts into final output."""
        final = ""
        effective_run_kwargs = dict(run_kwargs)
        if emit_stream and "run_config" not in effective_run_kwargs:
            effective_run_kwargs["run_config"] = build_run_config(
                profile="full",
                streaming=True,
                custom_metadata={
                    "channel": channel,
                    "request_kind": "gateway_stream",
                },
            )
        with route_context(channel, chat_id):
            async for event in runner.run_async(**effective_run_kwargs):
                text = extract_text(getattr(event, "content", None))
                merged = merge_text_stream(final, text)
                if emit_stream and merged and merged != final:
                    delta = merged[len(final):] if final and merged.startswith(final) else merged
                    if delta:
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=channel,
                                chat_id=chat_id,
                                content=delta,
                                metadata={"_stream_delta": True},
                            )
                        )
                final = merged
        if emit_stream:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content="",
                    metadata={"_stream_end": True},
                )
            )
        if final:
            return final
        if default_when_empty is None:
            return ""
        return default_when_empty

    async def _run_cron_job(self, job: CronJob) -> str | None:
        """Execute a scheduled cron job through the shared ADK runner."""
        target_channel = job.payload.channel or "local"
        target_chat_id = job.payload.to or "default"
        cron_step_id = f"cron:{job.id}"
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=target_channel,
                chat_id=target_chat_id,
                content=f"Cron job `{job.name}` started.",
                metadata={
                    **build_step_metadata(
                        step_phase="started",
                        step_update_kind="lifecycle",
                        step_title=f"Cron: {job.name}",
                        step_kind="runtime",
                        step_id=cron_step_id,
                        task_id=job.id,
                        session_id=f"cron:{job.id}",
                        tool_name="cron",
                        done=False,
                        content=f"Cron job `{job.name}` started.",
                    ),
                    "_feedback_origin": "runtime",
                },
            )
        )
        prompt = append_execution_time(job.payload.message)
        request = types.UserContent(parts=[types.Part.from_text(text=prompt)])
        principal = self._resolve_service_principal("cron")
        session_id = f"cron:{job.id}"
        session_route_key = self._session_route_key(
            channel=target_channel,
            chat_id=target_chat_id,
            principal_id=principal.principal_id,
        )
        interaction_context = self._build_interaction_context(
            principal=principal,
            channel=target_channel,
            chat_id=target_chat_id,
            session_id=session_id,
            session_route_key=session_route_key,
        )
        state_delta = await self._build_state_delta(
            runner=self.runner,
            interaction_context=interaction_context,
        )
        final = await self._run_text_stream(
            runner=self.runner,
            channel=target_channel,
            chat_id=target_chat_id,
            user_id=principal.principal_id,
            session_id=session_id,
            new_message=request,
            state_delta=state_delta,
        )
        if job.payload.deliver:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=target_channel,
                    chat_id=target_chat_id,
                    content=final,
                    metadata={
                        **build_step_metadata(
                            event_class="step_output",
                            step_phase="finished",
                            step_update_kind="result",
                            step_title=f"Cron result: {job.name}",
                            step_kind="runtime",
                            step_id=cron_step_id,
                            task_id=job.id,
                            session_id=f"cron:{job.id}",
                            tool_name="cron",
                            done=True,
                            content=final,
                        ),
                        "_feedback_origin": "runtime",
                    },
                )
            )
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=target_channel,
                chat_id=target_chat_id,
                content=f"Cron job `{job.name}` finished.",
                metadata={
                    **build_step_metadata(
                        step_phase="finished",
                        step_update_kind="lifecycle",
                        step_title=f"Cron: {job.name}",
                        step_kind="runtime",
                        step_id=cron_step_id,
                        task_id=job.id,
                        session_id=f"cron:{job.id}",
                        tool_name="cron",
                        done=True,
                        content=f"Cron job `{job.name}` finished.",
                    ),
                    "_feedback_origin": "runtime",
                },
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
        configure_step_event_publisher(self.bus.publish_outbound)
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
        configure_step_event_publisher(None)
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
        principal = self._resolve_message_principal(msg)
        session_route_key = self._session_route_key(
            channel=msg.channel,
            chat_id=msg.chat_id,
            principal_id=principal.principal_id,
        )
        if command == "/new":
            active_session_id = self._session_overrides.get(session_route_key, session_route_key)
            await self._persist_session_memory_snapshot(
                user_id=principal.principal_id,
                session_id=active_session_id,
            )
            self._session_overrides[session_route_key] = f"{session_route_key}:new:{uuid.uuid4().hex[:12]}"
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Started a new conversation session.",
                metadata=msg.metadata,
            )

        active_session_id = self._session_overrides.get(session_route_key, session_route_key)
        interaction_context = self._build_interaction_context(
            principal=principal,
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_id=active_session_id,
            session_route_key=session_route_key,
        )
        state_delta = await self._build_state_delta(
            runner=self.runner,
            interaction_context=interaction_context,
        )
        request = self._build_user_request(
            text=msg.content,
            media_paths=msg.media,
            received_at=msg.timestamp,
        )
        # Route context lets tools like `message(...)` infer the current target.
        final = await self._run_text_stream(
            runner=self.runner,
            channel=msg.channel,
            chat_id=msg.chat_id,
            emit_stream=bool((msg.metadata or {}).get("_wants_stream")),
            user_id=principal.principal_id,
            session_id=active_session_id,
            new_message=request,
            state_delta=state_delta,
        )
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final,
            metadata={
                **(msg.metadata or {}),
                **({"_streamed": True} if (msg.metadata or {}).get("_wants_stream") else {}),
            },
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
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=request.channel,
                    chat_id=request.chat_id,
                    content="Sub-agent execution started.",
                    metadata={
                        **build_step_metadata(
                            step_phase="running",
                            step_update_kind="progress",
                            step_title="Sub-agent running",
                            step_kind="subagent",
                            invocation_id=request.invocation_id,
                            function_call_id=request.function_call_id,
                            step_id=request.task_id,
                            task_id=request.task_id,
                            tool_name="spawn_subagent",
                            done=False,
                            content="Sub-agent execution started.",
                        ),
                        "_feedback_origin": "runtime",
                    },
                )
            )
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
        completed = response_payload.get("status") == "completed"
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
        base_metadata = {
            **build_step_metadata(
                step_phase="finished" if completed else "failed",
                step_update_kind="lifecycle",
                step_title="Sub-agent completed" if completed else "Sub-agent failed",
                step_kind="subagent",
                invocation_id=request.invocation_id,
                function_call_id=request.function_call_id,
                step_id=request.task_id,
                task_id=request.task_id,
                tool_name="spawn_subagent",
                done=True,
                important=not completed,
                content=content,
            ),
            "_feedback_origin": "runtime",
        }
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=request.channel,
                chat_id=request.chat_id,
                content=content,
                metadata=base_metadata,
            )
        )
        if completed:
            result_text = str(response_payload.get("result", "") or "").strip()
            if result_text:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=request.channel,
                        chat_id=request.chat_id,
                        content=result_text,
                        metadata={
                            **build_step_metadata(
                                event_class="step_output",
                                step_phase="finished",
                                step_update_kind="result",
                                step_title="Sub-agent result",
                                step_kind="subagent",
                                invocation_id=request.invocation_id,
                                function_call_id=request.function_call_id,
                                step_id=request.task_id,
                                task_id=request.task_id,
                                tool_name="spawn_subagent",
                                done=True,
                                content=result_text,
                            ),
                            "_feedback_origin": "runtime",
                        },
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
