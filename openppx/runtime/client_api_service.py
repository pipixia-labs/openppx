"""Local HTTP + SSE client API service for openppx."""

from __future__ import annotations

import datetime as dt
import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..core.config import get_data_dir
from ..core.logging_utils import debug_logging_enabled, emit_debug
from .access_policy import AccessPolicy
from .agent_access_runtime import ensure_access_principal
from .agent_access_runtime import ensure_agent_access_record
from .agent_access_store import AgentAccessStore, AgentMembership, AgentRecord
from .identity_models import ResolvedPrincipal
from .identity_store import IdentityStore
from .memory_query_service import MemoryQueryService
from .memory_shared import memory_entry_text
from .session_service import SessionConfig, create_session_service
from .sqlite_memory_service import SQLiteMemoryService


def _iso_now() -> str:
    """Return the current timestamp as an ISO 8601 string."""

    return dt.datetime.now().astimezone().isoformat()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    """Encode one JSON payload using UTF-8."""

    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    """Build a success envelope."""

    return {"ok": True, "data": data}


def _error(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an error envelope."""

    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
    }


def _normalize_agent_name(value: str) -> str:
    """Normalize one agent id using the existing filesystem-safe convention."""

    normalized = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip().lower())
    return normalized.strip("-_")


_MUTATION_AUDIT_ACTIONS = (
    "set_owner",
    "upsert_membership",
    "delete_membership",
    "batch_add_participants",
    "batch_remove_participants",
    "sync_participants",
)


def _normalize_principal_id_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize one list of principal ids while preserving stable order."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values or ():
        principal_id = str(raw or "").strip()
        if not principal_id or principal_id in seen:
            continue
        seen.add(principal_id)
        normalized.append(principal_id)
    return normalized


def _normalize_access_audit_category(value: str | None) -> str:
    """Normalize one admin-audit category selector."""
    normalized = str(value or "all").strip().lower()
    if normalized in {"", "all", "admin"}:
        return "all"
    if normalized == "mutation":
        return "mutation"
    raise ValueError("Query parameter 'category' must be 'all' or 'mutation'.")


def _actions_for_access_audit_category(category: str) -> tuple[str, ...] | None:
    """Return the audit actions included in one category filter."""
    normalized = _normalize_access_audit_category(category)
    if normalized == "all":
        return None
    return _MUTATION_AUDIT_ACTIONS


def global_config_path(data_dir: Path | None = None) -> Path:
    """Return the global multi-agent config path."""

    root = data_dir or get_data_dir()
    return root / "global_config.json"


def agent_config_path(agent_name: str, data_dir: Path | None = None) -> Path:
    """Return the per-agent config path."""

    root = data_dir or get_data_dir()
    return root / agent_name / "config.json"


def list_enabled_agent_names(data_dir: Path | None = None) -> list[str]:
    """Read enabled agent names from the global config file."""

    path = global_config_path(data_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []
    agents_raw = raw.get("agents")
    if isinstance(agents_raw, list):
        entries = agents_raw
    elif isinstance(agents_raw, dict) and isinstance(agents_raw.get("list"), list):
        entries = agents_raw["list"]
    else:
        entries = []

    names: list[str] = []
    seen: set[str] = set()
    for item in entries:
        enabled = True
        if isinstance(item, str):
            name = _normalize_agent_name(item)
        elif isinstance(item, dict):
            name = _normalize_agent_name(str(item.get("name") or item.get("id") or ""))
            raw_enabled = item.get("enabled")
            enabled = raw_enabled is not False
        else:
            continue
        if not name or not enabled or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def build_agent_profile(agent_name: str, data_dir: Path | None = None) -> dict[str, Any]:
    """Build one client-facing agent profile from config files."""

    config_path = agent_config_path(agent_name, data_dir)
    cfg = _read_json_file(config_path) or {}
    agent_cfg = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    workspace = str(agent_cfg.get("workspace") or "").strip()
    description = f"Workspace: {workspace}" if workspace else "Local openppx agent"
    return {
        "id": agent_name,
        "name": agent_name,
        "description": description,
        "enabled": True,
        "status": "healthy" if config_path.exists() else "disabled",
        "workspace": workspace or None,
        "avatar": None,
        "tags": ["local", "openppx"],
    }


def _run_worker_command(*, config_path: Path, args: list[str]) -> dict[str, Any]:
    """Run one worker action and parse its final NDJSON line."""

    cmd = [sys.executable, "-m", "openppx.runtime.client_api_worker", *args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(config_path.parent),
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or f"worker exited with code {proc.returncode}"
        raise RuntimeError(message)
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return {}
    return json.loads(lines[-1])


def _session_db_url_for_config_path(config_path: Path) -> str:
    """Build the per-agent SQLite session DB URL without mutating process env."""

    db_path = config_path.parent / "database" / "sessions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path}"


def _preview_value(value: Any, fallback: str) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or fallback
    try:
        dumped = json.dumps(value if value is not None else {}, ensure_ascii=False, indent=2)
    except Exception:
        dumped = str(value)
    dumped = dumped.strip()
    if not dumped or dumped == "{}":
        return fallback
    return dumped[:320] + ("..." if len(dumped) > 320 else "")


def _strip_request_time_prefix(text: str) -> str:
    """Remove runtime-injected request-time guidance from persisted user text."""

    stripped = text.strip()
    if not stripped.startswith("Current request time: "):
        return text

    lines = stripped.splitlines()
    if len(lines) < 2 or "Use this as the reference 'now' for relative time expressions" not in lines[1]:
        return text

    body_lines = lines[2:]
    while body_lines and not body_lines[0].strip():
        body_lines = body_lines[1:]
    return "\n".join(body_lines).strip()


def _step_ref_payload(*, step_id: str, title: str, status: str, detail: str) -> dict[str, Any]:
    """Build one client-facing step part payload."""

    return {
        "type": "step_ref",
        "step_id": step_id,
        "title": title,
        "status": status,
        "detail": detail,
    }


def _message_payload(
    *,
    message_id: str,
    session_id: str,
    role: str,
    parts: list[dict[str, Any]],
    status: str,
) -> dict[str, Any]:
    """Build one client-facing message payload."""

    return {
        "id": message_id,
        "session_id": session_id,
        "role": role,
        "parts": parts,
        "status": status,
        "created_at": _iso_now(),
        "metadata": {},
    }


def _error_part_payload(*, code: str, text: str) -> dict[str, Any]:
    """Build one client-facing error part payload."""

    return {
        "type": "error",
        "error_code": code,
        "text": text,
    }


def _tool_result_payload(*, tool_name: str, summary: str, detail: str, raw_text: str) -> dict[str, Any]:
    """Build one client-facing tool result part payload."""

    return {
        "type": "tool_result",
        "tool_name": tool_name,
        "summary": summary,
        "detail": detail,
        "raw_text": raw_text,
    }


def _tool_result_summary(tool_name: str, response: Any) -> str:
    """Build a short human-readable summary for one tool response."""

    if isinstance(response, dict):
        message = response.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        summary = response.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
        ok = response.get("ok")
        if isinstance(ok, bool):
            return f"{tool_name} returned successfully." if ok else f"{tool_name} reported a failure."
        keys = list(response.keys())
        if keys:
            return f"{tool_name} returned {len(keys)} fields."
    if isinstance(response, str) and response.strip():
        return response.strip()[:140]
    return f"{tool_name} returned a result."


def _event_preview_text(event: dict[str, Any]) -> str:
    """Build a lightweight session preview string from one serialized event."""

    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    raw_parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    texts: list[str] = []
    for raw_part in raw_parts:
        if not isinstance(raw_part, dict):
            continue
        if bool(raw_part.get("thought")):
            continue
        text = raw_part.get("text")
        if isinstance(text, str) and text.strip():
            normalized_text = _strip_request_time_prefix(text)
            if normalized_text.strip():
                texts.append(normalized_text.strip())
    return " ".join(texts).strip()


def _compact_session_title(text: str, *, limit: int = 64) -> str:
    """Return a single-line session title derived from user-visible text."""
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _session_title_from_events(events: list[dict[str, Any]]) -> str:
    """Return the first user message as the stable client-facing session title."""
    for event in events:
        if str(event.get("author") or "").strip().lower() != "user":
            continue
        title = _compact_session_title(_event_preview_text(event))
        if title:
            return title
    return ""


def _debug(tag: str, payload: Any) -> None:
    """Emit one structured debug log when client-api debugging is enabled."""

    if not debug_logging_enabled():
        return
    emit_debug(tag, payload, depth=3)


def project_session_event(event: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    """Project one ADK session event into the client chat message schema."""

    author = str(event.get("author") or "").strip().lower()
    role = "assistant"
    if author == "user":
        role = "user"
    elif author == "tool":
        role = "tool"
    elif author == "system":
        role = "system"

    timestamp = event.get("timestamp")
    if isinstance(timestamp, (int, float)):
        created_at = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).astimezone().isoformat()
    else:
        created_at = _iso_now()

    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    raw_parts = content.get("parts") if isinstance(content, dict) and isinstance(content.get("parts"), list) else []
    parts: list[dict[str, Any]] = []
    for raw_part in raw_parts:
        if not isinstance(raw_part, dict):
            continue
        if bool(raw_part.get("thought")):
            continue
        text = raw_part.get("text")
        if isinstance(text, str) and text.strip():
            normalized_text = _strip_request_time_prefix(text)
            if normalized_text.strip():
                parts.append({"type": "markdown", "text": normalized_text})
        function_call = raw_part.get("function_call")
        if isinstance(function_call, dict):
            parts.append(
                {
                    "type": "step_ref",
                    "step_id": str(function_call.get("id") or "step"),
                    "title": str(function_call.get("name") or "tool"),
                    "status": "completed",
                    "detail": _preview_value(function_call.get("args"), "No tool arguments"),
                }
            )
        function_response = raw_part.get("function_response")
        if isinstance(function_response, dict):
            step_id = str(function_response.get("id") or function_response.get("name") or "tool")
            tool_name = str(function_response.get("name") or "tool")
            response = function_response.get("response") or {}
            parts.append(
                {
                    "type": "step_ref",
                    "step_id": step_id,
                    "title": tool_name,
                    "status": "completed",
                    "detail": _preview_value(response, "Tool returned without a payload"),
                }
            )
            parts.append(
                _tool_result_payload(
                    tool_name=tool_name,
                    summary=_tool_result_summary(tool_name, response),
                    detail=_preview_value(response, "Tool returned without a payload"),
                    raw_text=json.dumps(response, ensure_ascii=False, indent=2),
                )
            )
    if not parts:
        return None
    return {
        "id": str(event.get("id") or f"msg_{session_id}"),
        "session_id": session_id,
        "role": role,
        "parts": parts,
        "status": "completed",
        "created_at": created_at,
        "metadata": {},
    }


@dataclass(slots=True)
class RunEnvelope:
    """One replayable SSE event payload."""

    event_id: str
    seq: int
    event: str
    payload: dict[str, Any]


@dataclass(slots=True)
class _TimedCacheEntry:
    """One short-lived in-memory cache entry."""

    value: Any
    expires_at: float


class RunHandle:
    """Track one running worker subprocess and its replayable SSE events."""

    def __init__(self, *, run_id: str, agent_id: str, session_id: str, process: subprocess.Popen[str]) -> None:
        self.run_id = run_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.process = process
        self.assistant_message_id = f"msg_{run_id}_assistant"
        self._history: list[RunEnvelope] = []
        self._subscribers: list[queue.Queue[RunEnvelope | None]] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._stderr_lines: list[str] = []
        self.done = threading.Event()
        self.failed = False

    def publish(self, event: str, payload: dict[str, Any]) -> None:
        """Store and fan out one SSE event."""

        with self._lock:
            self._seq += 1
            envelope = RunEnvelope(
                event_id=f"{self.run_id}:{self._seq}",
                seq=self._seq,
                event=event,
                payload=payload,
            )
            self._history.append(envelope)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(envelope)

    def finish(self) -> None:
        """Mark the run as completed and close subscribers."""

        with self._lock:
            self.done.set()
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            subscriber.put(None)

    def append_stderr_line(self, line: str) -> None:
        """Retain a bounded stderr history for later debug reporting."""

        with self._lock:
            self._stderr_lines.append(line)
            if len(self._stderr_lines) > 20:
                self._stderr_lines = self._stderr_lines[-20:]

    def stderr_text(self) -> str:
        """Return the retained stderr snapshot."""

        with self._lock:
            return "\n".join(self._stderr_lines)

    def cancel(self) -> bool:
        """Terminate the subprocess if it is still running."""

        if self.done.is_set():
            return False
        if self.process.poll() is None:
            self.process.terminate()
        self.publish(
            "message.cancelled",
            {
                "run_id": self.run_id,
                "agent_id": self.agent_id,
                "session_id": self.session_id,
                "message_id": self.assistant_message_id,
                "status": "cancelled",
            },
        )
        self.publish(
            "run.cancelled",
            {
                "run_id": self.run_id,
                "agent_id": self.agent_id,
                "session_id": self.session_id,
                "message_id": self.assistant_message_id,
                "status": "cancelled",
            },
        )
        self.finish()
        return True

    def subscribe(self, last_event_id: str | None = None) -> queue.Queue[RunEnvelope | None]:
        """Create one subscriber queue and replay retained history."""

        q: queue.Queue[RunEnvelope | None] = queue.Queue()
        with self._lock:
            replay = list(self._history)
            if last_event_id:
                replay = [item for item in replay if item.event_id > last_event_id]
            if not self.done.is_set():
                self._subscribers.append(q)
            done = self.done.is_set()
        for item in replay:
            q.put(item)
        if done:
            q.put(None)
        return q


class ClientApiCoordinator:
    """Coordinate local client-facing HTTP requests and background run streams."""

    _CACHE_TTL_SECONDS = 5.0

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        identity_store: IdentityStore | None = None,
        agent_access_store: AgentAccessStore | None = None,
        access_policy: AccessPolicy | None = None,
        memory_query_service: MemoryQueryService | None = None,
    ) -> None:
        self.data_dir = data_dir or get_data_dir()
        default_identity_db_path = self.data_dir / "database" / "identity.db"
        self._identity_store = identity_store or IdentityStore(db_path=default_identity_db_path)
        self._agent_access_store = agent_access_store or AgentAccessStore(db_path=default_identity_db_path)
        self._access_policy = access_policy or AccessPolicy(
            identity_store=self._identity_store,
            agent_access_store=self._agent_access_store,
        )
        if memory_query_service is None:
            local_memory_db_path = self.data_dir / "database" / "memory.db"
            self._memory_query_service = MemoryQueryService(
                identity_store=self._identity_store,
                access_policy=self._access_policy,
                memory_service=SQLiteMemoryService(db_path=local_memory_db_path),
                audit_db_path=local_memory_db_path,
            )
        else:
            self._memory_query_service = memory_query_service
        self._session_agents: dict[str, str] = {}
        self._session_owners: dict[str, str] = {}
        self._runs: dict[str, RunHandle] = {}
        self._lock = threading.Lock()
        self._sessions_cache: dict[tuple[str, str], _TimedCacheEntry] = {}
        self._messages_cache: dict[tuple[str, str], _TimedCacheEntry] = {}

    def _ensure_requester_principal(self, user_id: str) -> ResolvedPrincipal:
        """Return a persisted requester principal for client-api operations."""
        principal_id = str(user_id or "ppx-client-user").strip() or "ppx-client-user"
        existing = self._identity_store.get_principal(principal_id)
        if existing is not None:
            return existing
        principal = ResolvedPrincipal(
            principal_id=principal_id,
            principal_type="human",
            privilege_level="minimal",
            account_kind="local_client",
            display_name=principal_id,
            authenticated=True,
            external_subject_id=principal_id,
            external_display_id=principal_id,
            metadata={"source": "client_api"},
        )
        return self._identity_store.put_principal(principal)

    def _ensure_agent_access_state(self, agent_id: str) -> Path | None:
        """Ensure access rows exist for one configured agent before evaluation."""
        config_path = agent_config_path(agent_id, self.data_dir)
        if not config_path.exists():
            return None
        ensure_agent_access_record(
            agent_id=agent_id,
            agent_name=agent_id,
            identity_store=self._identity_store,
            agent_access_store=self._agent_access_store,
            config_path=config_path,
            apply_env_overrides=False,
        )
        return config_path

    def _visible_principal_ids(self, requester_principal_id: str, *, agent_id: str, access_kind: str) -> tuple[Any, tuple[str, ...]]:
        """Resolve the effective visible principal ids for one request."""
        decision = self._access_policy.decide_agent_scope(
            requester_principal_id=requester_principal_id,
            agent_id=agent_id,
            access_kind=access_kind,
        )
        if not decision.allow:
            return decision, ()
        visible_principal_ids = decision.resolved_scope(self._identity_store.list_principal_ids())
        if visible_principal_ids:
            return decision, visible_principal_ids
        return decision, (requester_principal_id,)

    def _read_cache(self, cache: dict[tuple[str, str], _TimedCacheEntry], key: tuple[str, str]) -> Any | None:
        now_ts = dt.datetime.now().timestamp()
        with self._lock:
            entry = cache.get(key)
            if entry is None:
                return None
            if entry.expires_at < now_ts:
                cache.pop(key, None)
                return None
            return entry.value

    def _write_cache(self, cache: dict[tuple[str, str], _TimedCacheEntry], key: tuple[str, str], value: Any) -> None:
        with self._lock:
            cache[key] = _TimedCacheEntry(
                value=value,
                expires_at=dt.datetime.now().timestamp() + self._CACHE_TTL_SECONDS,
            )

    def _invalidate_agent_cache(self, agent_id: str, *, user_id: str) -> None:
        with self._lock:
            self._sessions_cache.pop((agent_id, user_id), None)

    def _invalidate_session_cache(self, session_id: str, *, user_id: str) -> None:
        with self._lock:
            self._messages_cache.pop((session_id, user_id), None)

    def _invalidate_agent_access_caches(self, agent_id: str) -> None:
        """Drop cached views that may become stale after access mutations."""
        with self._lock:
            self._sessions_cache = {
                key: value for key, value in self._sessions_cache.items() if key[0] != agent_id
            }
            affected_session_ids = {
                session_id
                for session_id, cached_agent_id in self._session_agents.items()
                if cached_agent_id == agent_id
            }
            self._messages_cache = {
                key: value for key, value in self._messages_cache.items() if key[0] not in affected_session_ids
            }

    def _record_admin_audit(
        self,
        *,
        agent_id: str,
        requester: ResolvedPrincipal,
        action: str,
        relation_to_agent: str,
        target_principal_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Persist one admin-surface audit event without raising to callers."""
        try:
            self._agent_access_store.record_audit(
                agent_id=agent_id,
                actor_principal_id=requester.principal_id,
                actor_relation=relation_to_agent,
                action=action,
                target_principal_id=target_principal_id,
                details=details,
            )
        except Exception:
            return

    def _validate_membership_management(
        self,
        *,
        agent_id: str,
        requester: ResolvedPrincipal,
        access_kind: str = "membership_write",
        denied_action: str,
        denied_target_principal_id: str = "",
        denied_details: dict[str, Any] | None = None,
    ) -> tuple[Path | None, Any, dict[str, Any] | None]:
        """Validate one membership-management request and prebuild deny payloads."""
        config_path = self._ensure_agent_access_state(agent_id)
        if config_path is None:
            return None, None, _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        decision = self._access_policy.decide_agent_management(
            requester_principal_id=requester.principal_id,
            agent_id=agent_id,
            access_kind=access_kind,
        )
        if decision.allow:
            return config_path, decision, None
        self._record_admin_audit(
            agent_id=agent_id,
            requester=requester,
            action=denied_action,
            relation_to_agent=decision.relation_to_agent,
            target_principal_id=denied_target_principal_id,
            details={
                "allowed": False,
                "reason": decision.reason,
                **dict(denied_details or {}),
            },
        )
        return config_path, decision, _error(
            "ACCESS_DENIED",
            f"Principal '{requester.principal_id}' cannot change memberships for agent '{agent_id}'.",
            {"reason": decision.reason},
        )

    def _read_sessions_direct(self, config_path: Path, *, user_id: str) -> list[dict[str, Any]]:
        """Read session summaries directly from the per-agent SQLite store."""

        async def _load() -> list[dict[str, Any]]:
            service = create_session_service(SessionConfig(db_url=_session_db_url_for_config_path(config_path)))
            async with service:
                response = await service.list_sessions(app_name="openppx", user_id=user_id)
                items: list[dict[str, Any]] = []
                for session in response.sessions:
                    detail = await service.get_session(
                        app_name="openppx",
                        user_id=user_id,
                        session_id=session.id,
                    )
                    events = [event.model_dump(mode="json") for event in (detail.events if detail else [])]
                    items.append(
                        {
                            "id": session.id,
                            "last_update_time": (detail.last_update_time if detail else session.last_update_time),
                            "title": _session_title_from_events(events),
                            "last_preview": _event_preview_text(events[-1]) if events else "",
                        }
                    )
                return items

        return asyncio.run(_load())

    def _read_sessions_worker(self, config_path: Path, *, user_id: str) -> list[dict[str, Any]]:
        """Read session summaries through the worker fallback path."""
        response = _run_worker_command(
            config_path=config_path,
            args=[
                "list_sessions",
                "--config-path",
                str(config_path),
                "--user-id",
                user_id,
            ],
        )
        return [item for item in response.get("sessions", []) if isinstance(item, dict)]

    def _create_session_direct(self, config_path: Path, *, user_id: str, session_id: str) -> dict[str, Any]:
        """Create one session directly in the per-agent SQLite store."""

        async def _create() -> dict[str, Any]:
            service = create_session_service(SessionConfig(db_url=_session_db_url_for_config_path(config_path)))
            async with service:
                session = await service.create_session(
                    app_name="openppx",
                    user_id=user_id,
                    session_id=session_id,
                )
            return {
                "id": session.id,
                "last_update_time": session.last_update_time,
            }

        return asyncio.run(_create())

    def _get_session_direct(self, config_path: Path, *, user_id: str, session_id: str) -> dict[str, Any] | None:
        """Read one session with events directly from the per-agent SQLite store."""

        async def _load() -> dict[str, Any] | None:
            service = create_session_service(SessionConfig(db_url=_session_db_url_for_config_path(config_path)))
            async with service:
                session = await service.get_session(
                    app_name="openppx",
                    user_id=user_id,
                    session_id=session_id,
                )
            if session is None:
                return None
            return {
                "id": session.id,
                "last_update_time": session.last_update_time,
                "events": [event.model_dump(mode="json") for event in session.events],
            }

        return asyncio.run(_load())

    def _get_session_worker(self, config_path: Path, *, user_id: str, session_id: str) -> dict[str, Any] | None:
        """Read one session through the worker fallback path."""
        response = _run_worker_command(
            config_path=config_path,
            args=[
                "get_session",
                "--config-path",
                str(config_path),
                "--session-id",
                session_id,
                "--user-id",
                user_id,
            ],
        )
        session = response.get("session")
        return session if isinstance(session, dict) else None

    def _read_sessions_for_principal(self, config_path: Path, *, user_id: str) -> list[dict[str, Any]]:
        """Read one principal-scoped session list with worker fallback."""
        try:
            return self._read_sessions_direct(config_path, user_id=user_id)
        except Exception as exc:
            _debug(
                "client_api.list_sessions.direct_failed",
                {
                    "config_path": str(config_path),
                    "user_id": user_id,
                    "error": str(exc),
                },
            )
            return self._read_sessions_worker(config_path, user_id=user_id)

    def _get_session_for_principal(
        self,
        config_path: Path,
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Read one principal-scoped session with worker fallback."""
        try:
            return self._get_session_direct(config_path, user_id=user_id, session_id=session_id)
        except Exception as exc:
            _debug(
                "client_api.get_session.direct_failed",
                {
                    "config_path": str(config_path),
                    "session_id": session_id,
                    "user_id": user_id,
                    "error": str(exc),
                },
            )
            return self._get_session_worker(config_path, user_id=user_id, session_id=session_id)

    def _collect_visible_sessions(
        self,
        *,
        agent_id: str,
        requester_principal_id: str,
    ) -> tuple[Any, list[tuple[str, dict[str, Any]]]] | dict[str, Any]:
        """Collect session rows visible to one requester for one agent."""
        config_path = self._ensure_agent_access_state(agent_id)
        if config_path is None:
            return _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        decision, visible_principal_ids = self._visible_principal_ids(
            requester_principal_id,
            agent_id=agent_id,
            access_kind="session_list",
        )
        if not decision.allow:
            return _error(
                "ACCESS_DENIED",
                f"Principal '{requester_principal_id}' cannot list sessions for agent '{agent_id}'.",
                {"reason": decision.reason},
            )

        rows: list[tuple[str, dict[str, Any]]] = []
        for subject_principal_id in visible_principal_ids:
            try:
                sessions = self._read_sessions_for_principal(config_path, user_id=subject_principal_id)
            except Exception as exc:
                return _error("RUNTIME_UNAVAILABLE", str(exc))
            for session in sessions:
                rows.append((subject_principal_id, session))
        return decision, rows

    def _find_session_owner(
        self,
        *,
        session_id: str,
        requester_principal_id: str,
    ) -> tuple[str, str] | dict[str, Any]:
        """Resolve the agent id and owner principal for one visible session."""
        agent_id = self._session_agents.get(session_id)
        subject_principal_id = self._session_owners.get(session_id)
        if agent_id and subject_principal_id:
            decision = self._access_policy.decide_subject_access(
                requester_principal_id=requester_principal_id,
                agent_id=agent_id,
                subject_principal_id=subject_principal_id,
                access_kind="session_read",
            )
            if decision.allow:
                return agent_id, subject_principal_id
            return _error(
                "ACCESS_DENIED",
                f"Principal '{requester_principal_id}' cannot read session '{session_id}'.",
                {"reason": decision.reason},
            )

        for candidate in list_enabled_agent_names(self.data_dir):
            visible = self._collect_visible_sessions(
                agent_id=candidate,
                requester_principal_id=requester_principal_id,
            )
            if isinstance(visible, dict):
                if visible.get("error", {}).get("code") == "ACCESS_DENIED":
                    continue
                return visible
            _decision, rows = visible
            for owner_principal_id, session in rows:
                candidate_session_id = str(session.get("id") or "")
                if candidate_session_id != session_id:
                    continue
                self._session_agents[session_id] = candidate
                self._session_owners[session_id] = owner_principal_id
                return candidate, owner_principal_id
        return _error("SESSION_NOT_FOUND", f"Session '{session_id}' was not found.")

    def health(self) -> dict[str, Any]:
        """Return a lightweight health payload."""

        return _ok(
            {
                "service": "openppx-client-api",
                "state": "healthy",
                "data_dir": str(self.data_dir),
                "agents": len(list_enabled_agent_names(self.data_dir)),
                "timestamp": _iso_now(),
            }
        )

    def runtime_status(self) -> dict[str, Any]:
        """Return a client-facing runtime status payload."""

        return _ok(
            {
                "target": {
                    "id": "local-default",
                    "type": "local",
                    "name": "This Mac",
                },
                "state": "healthy",
                "summary": "Local client-api gateway is ready.",
                "detail": "The desktop client can use HTTP for queries and SSE for run events.",
            }
        )

    def list_agents(self) -> dict[str, Any]:
        """Return enabled local agent profiles."""

        agents = [build_agent_profile(name, self.data_dir) for name in list_enabled_agent_names(self.data_dir)]
        return _ok({"items": agents})

    def list_sessions(self, agent_id: str, *, user_id: str = "ppx-client-user") -> dict[str, Any]:
        """Return projected session summaries for one agent."""

        requester = self._ensure_requester_principal(user_id)
        config_path = agent_config_path(agent_id, self.data_dir)
        if not config_path.exists():
            return _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        cache_key = (agent_id, requester.principal_id)
        cached = self._read_cache(self._sessions_cache, cache_key)
        if cached is not None:
            _debug(
                "client_api.list_sessions.cache_hit",
                {
                    "agent_id": agent_id,
                    "user_id": requester.principal_id,
                    "count": len(cached),
                },
            )
            return _ok({"items": cached})
        visible = self._collect_visible_sessions(
            agent_id=agent_id,
            requester_principal_id=requester.principal_id,
        )
        if isinstance(visible, dict):
            return visible
        _decision, session_rows = visible
        items = []
        for subject_principal_id, session in session_rows:
            session_id = str(session.get("id") or "")
            if not session_id:
                continue
            self._session_agents[session_id] = agent_id
            self._session_owners[session_id] = subject_principal_id
            updated_raw = session.get("last_update_time")
            if isinstance(updated_raw, (int, float)):
                updated_at = dt.datetime.fromtimestamp(updated_raw, tz=dt.timezone.utc).astimezone().isoformat()
            else:
                updated_at = _iso_now()
            items.append(
                {
                    "id": session_id,
                    "agent_id": agent_id,
                    "subject_principal_id": subject_principal_id,
                    "title": str(session.get("title") or "").strip() or f"Session {session_id[:8]}",
                    "updated_at": updated_at,
                    "last_message_preview": str(session.get("last_preview") or "OpenPPX session"),
                    "archived": False,
                }
            )
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        self._write_cache(self._sessions_cache, cache_key, items)
        return _ok({"items": items})

    def create_session(self, agent_id: str, *, user_id: str = "ppx-client-user") -> dict[str, Any]:
        """Create one session for the target agent."""

        requester = self._ensure_requester_principal(user_id)
        config_path = self._ensure_agent_access_state(agent_id)
        if config_path is None:
            return _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        session_id = f"{agent_id}-{os.urandom(8).hex()}"
        try:
            session = self._create_session_direct(
                config_path,
                user_id=requester.principal_id,
                session_id=session_id,
            )
        except Exception as exc:
            _debug(
                "client_api.create_session.direct_failed",
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "error": str(exc),
                },
            )
            try:
                response = _run_worker_command(
                    config_path=config_path,
                    args=[
                        "create_session",
                        "--config-path",
                        str(config_path),
                        "--session-id",
                        session_id,
                        "--user-id",
                        requester.principal_id,
                    ],
                )
                session = response.get("session") if isinstance(response.get("session"), dict) else {}
            except Exception as fallback_exc:
                return _error("RUNTIME_UNAVAILABLE", str(fallback_exc))
        session_id = str(session.get("id") or session_id)
        self._session_agents[session_id] = agent_id
        self._session_owners[session_id] = requester.principal_id
        self._invalidate_agent_cache(agent_id, user_id=requester.principal_id)
        self._invalidate_session_cache(session_id, user_id=requester.principal_id)
        updated_raw = session.get("last_update_time")
        if isinstance(updated_raw, (int, float)):
            updated_at = dt.datetime.fromtimestamp(updated_raw, tz=dt.timezone.utc).astimezone().isoformat()
        else:
            updated_at = _iso_now()
        return _ok(
            {
                "session": {
                    "id": session_id,
                    "agent_id": agent_id,
                    "subject_principal_id": requester.principal_id,
                    "title": "新对话",
                    "updated_at": updated_at,
                    "last_message_preview": "",
                    "archived": False,
                }
            }
        )

    def get_session_messages(self, session_id: str, *, user_id: str = "ppx-client-user") -> dict[str, Any]:
        """Return projected message history for one session."""

        requester = self._ensure_requester_principal(user_id)
        cache_key = (session_id, requester.principal_id)
        cached = self._read_cache(self._messages_cache, cache_key)
        if cached is not None:
            _debug(
                "client_api.get_session.cache_hit",
                {
                    "session_id": session_id,
                    "user_id": requester.principal_id,
                    "count": len(cached),
                },
            )
            return _ok({"items": cached})
        location = self._find_session_owner(
            session_id=session_id,
            requester_principal_id=requester.principal_id,
        )
        if isinstance(location, dict):
            return location
        agent_id, subject_principal_id = location
        config_path = agent_config_path(agent_id, self.data_dir)
        try:
            session = self._get_session_for_principal(
                config_path,
                user_id=subject_principal_id,
                session_id=session_id,
            )
        except Exception as exc:
            return _error("RUNTIME_UNAVAILABLE", str(exc))
        if session is None:
            return _error("SESSION_NOT_FOUND", f"Session '{session_id}' was not found.")
        events = session.get("events") if isinstance(session.get("events"), list) else []
        messages = [
            message
            for event in events
            if isinstance(event, dict)
            for message in [project_session_event(event, session_id)]
            if message is not None
        ]
        for message in messages:
            metadata = message.setdefault("metadata", {})
            metadata["subject_principal_id"] = subject_principal_id
        self._write_cache(self._messages_cache, cache_key, messages)
        return _ok({"items": messages})

    def get_agent_access(self, agent_id: str, *, user_id: str = "ppx-client-user") -> dict[str, Any]:
        """Return the requester's visible access snapshot for one agent."""

        requester = self._ensure_requester_principal(user_id)
        if self._ensure_agent_access_state(agent_id) is None:
            return _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        decision = self._access_policy.decide_agent_scope(
            requester_principal_id=requester.principal_id,
            agent_id=agent_id,
            access_kind="agent_access_read",
        )
        if not decision.allow:
            self._record_admin_audit(
                agent_id=agent_id,
                requester=requester,
                action="read_access",
                relation_to_agent=decision.relation_to_agent,
                details={"allowed": False, "reason": decision.reason},
            )
            return _error(
                "ACCESS_DENIED",
                f"Principal '{requester.principal_id}' cannot read access state for agent '{agent_id}'.",
                {"reason": decision.reason},
            )

        record = self._agent_access_store.get_agent_record(agent_id)
        if record is None:
            return _error("RUNTIME_UNAVAILABLE", f"Agent '{agent_id}' access record is unavailable.")

        visible_principal_ids = set(decision.resolved_scope(self._identity_store.list_principal_ids()))
        memberships = []
        for membership in self._agent_access_store.list_memberships(agent_id=agent_id):
            if membership.principal_id not in visible_principal_ids and decision.scope_kind != "all":
                continue
            principal = self._identity_store.get_principal(membership.principal_id)
            memberships.append(
                {
                    "principal_id": membership.principal_id,
                    "relation": membership.relation,
                    "joined_at_ms": membership.joined_at_ms,
                    "metadata": dict(membership.metadata),
                    "display_name": principal.display_name if principal is not None else membership.principal_id,
                    "principal_type": principal.principal_type if principal is not None else "unknown",
                    "privilege_level": principal.privilege_level if principal is not None else "",
                }
            )

        owner_visible = bool(record.owner_principal_id) and decision.allows_principal(record.owner_principal_id)
        payload = _ok(
            {
                "agent": {
                    "id": record.agent_id,
                    "name": record.name,
                    "privilege_level": record.privilege_level,
                    "owner_principal_id": record.owner_principal_id if owner_visible else None,
                    "owner_configured": bool(record.owner_principal_id),
                    "status": record.status,
                    "config_ref": record.config_ref or None,
                    "metadata": dict(record.metadata),
                },
                "requester": {
                    "principal_id": requester.principal_id,
                    "relation": decision.relation_to_agent,
                    "reason": decision.reason,
                    "scope_kind": decision.scope_kind,
                    "capabilities": {
                        "can_manage_memberships": self._access_policy.decide_agent_management(
                            requester_principal_id=requester.principal_id,
                            agent_id=agent_id,
                            access_kind="membership_write",
                        ).allow,
                        "can_read_access_audit": self._access_policy.decide_agent_management(
                            requester_principal_id=requester.principal_id,
                            agent_id=agent_id,
                            access_kind="access_audit_read",
                        ).allow,
                        "can_read_admin_audit": self._access_policy.decide_agent_management(
                            requester_principal_id=requester.principal_id,
                            agent_id=agent_id,
                            access_kind="access_audit_read",
                        ).allow,
                        "can_change_owner": self._access_policy.decide_agent_management(
                            requester_principal_id=requester.principal_id,
                            agent_id=agent_id,
                            access_kind="ownership_write",
                        ).allow,
                    },
                },
                "memberships": memberships,
            }
        )
        self._record_admin_audit(
            agent_id=agent_id,
            requester=requester,
            action="read_access",
            relation_to_agent=decision.relation_to_agent,
            details={
                "allowed": True,
                "reason": decision.reason,
                "visible_membership_count": len(memberships),
                "owner_visible": owner_visible,
            },
        )
        return payload

    def set_agent_owner(
        self,
        agent_id: str,
        owner_principal_id: str,
        *,
        user_id: str = "ppx-client-user",
    ) -> dict[str, Any]:
        """Set one agent owner through the managed access layer."""

        requester = self._ensure_requester_principal(user_id)
        if self._ensure_agent_access_state(agent_id) is None:
            return _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        decision = self._access_policy.decide_agent_management(
            requester_principal_id=requester.principal_id,
            agent_id=agent_id,
            access_kind="ownership_write",
        )
        if not decision.allow:
            self._record_admin_audit(
                agent_id=agent_id,
                requester=requester,
                action="set_owner",
                relation_to_agent=decision.relation_to_agent,
                target_principal_id=str(owner_principal_id or "").strip(),
                details={"allowed": False, "reason": decision.reason, "source": "client_api"},
            )
            return _error(
                "ACCESS_DENIED",
                f"Principal '{requester.principal_id}' cannot change owner for agent '{agent_id}'.",
                {"reason": decision.reason},
            )

        normalized_owner_principal_id = str(owner_principal_id or "").strip()
        if not normalized_owner_principal_id:
            return _error("INVALID_REQUEST", "Field 'owner_principal_id' is required.")

        owner_principal = ensure_access_principal(
            self._identity_store,
            principal_id=normalized_owner_principal_id,
            source="client_api_access_mutation",
            account_kind="managed_access",
        )
        record = self._agent_access_store.get_agent_record(agent_id)
        if owner_principal is None or record is None:
            return _error("RUNTIME_UNAVAILABLE", f"Agent '{agent_id}' access record is unavailable.")
        previous_owner_principal_id = record.owner_principal_id

        updated = self._agent_access_store.upsert_agent_record(
            AgentRecord(
                agent_id=record.agent_id,
                name=record.name,
                privilege_level=record.privilege_level,
                owner_principal_id=owner_principal.principal_id,
                status=record.status,
                config_ref=record.config_ref,
                metadata={
                    **dict(record.metadata),
                    "owner_source": "client_api",
                },
            )
        )
        self._agent_access_store.record_audit(
            agent_id=agent_id,
            actor_principal_id=requester.principal_id,
            actor_relation=decision.relation_to_agent,
            action="set_owner",
            target_principal_id=owner_principal.principal_id,
            details={
                "allowed": True,
                "reason": decision.reason,
                "previous_owner_principal_id": previous_owner_principal_id,
                "owner_principal_id": owner_principal.principal_id,
                "changed": previous_owner_principal_id != owner_principal.principal_id,
                "source": "client_api",
            },
        )
        self._invalidate_agent_access_caches(agent_id)
        return _ok(
            {
                "agent": {
                    "id": updated.agent_id,
                    "owner_principal_id": updated.owner_principal_id,
                    "metadata": dict(updated.metadata),
                }
            }
        )

    def upsert_agent_membership(
        self,
        agent_id: str,
        principal_id: str,
        *,
        relation: str = "participant",
        user_id: str = "ppx-client-user",
    ) -> dict[str, Any]:
        """Create or update one agent membership through the managed access layer."""

        requester = self._ensure_requester_principal(user_id)
        _config_path, decision, denied = self._validate_membership_management(
            agent_id=agent_id,
            requester=requester,
            denied_action="upsert_membership",
            denied_target_principal_id=str(principal_id or "").strip(),
            denied_details={"source": "client_api", "relation": str(relation or "").strip().lower()},
        )
        if denied is not None:
            return denied

        normalized_principal_id = str(principal_id or "").strip()
        normalized_relation = str(relation or "").strip().lower()
        if not normalized_principal_id:
            return _error("INVALID_REQUEST", "Field 'principal_id' is required.")
        if normalized_relation != "participant":
            return _error("INVALID_REQUEST", "Field 'relation' must currently be 'participant'.")

        principal = ensure_access_principal(
            self._identity_store,
            principal_id=normalized_principal_id,
            source="client_api_access_mutation",
            account_kind="managed_access",
        )
        if principal is None:
            return _error("RUNTIME_UNAVAILABLE", "Could not ensure the target principal.")
        previous_membership = self._agent_access_store.get_membership(
            agent_id=agent_id,
            principal_id=principal.principal_id,
        )

        membership = self._agent_access_store.upsert_membership(
            AgentMembership(
                agent_id=agent_id,
                principal_id=principal.principal_id,
                relation=normalized_relation,
                metadata={"source": "client_api"},
            )
        )
        self._agent_access_store.record_audit(
            agent_id=agent_id,
            actor_principal_id=requester.principal_id,
            actor_relation=decision.relation_to_agent,
            action="upsert_membership",
            target_principal_id=membership.principal_id,
            details={
                "allowed": True,
                "reason": decision.reason,
                "relation": membership.relation,
                "previous_relation": previous_membership.relation if previous_membership is not None else None,
                "changed": previous_membership is None
                or previous_membership.relation != membership.relation
                or dict(previous_membership.metadata) != dict(membership.metadata),
                "joined_at_ms": membership.joined_at_ms,
                "source": "client_api",
            },
        )
        self._invalidate_agent_access_caches(agent_id)
        return _ok(
            {
                "membership": {
                    "agent_id": membership.agent_id,
                    "principal_id": membership.principal_id,
                    "relation": membership.relation,
                    "joined_at_ms": membership.joined_at_ms,
                    "metadata": dict(membership.metadata),
                }
            }
        )

    def delete_agent_membership(
        self,
        agent_id: str,
        principal_id: str,
        *,
        user_id: str = "ppx-client-user",
    ) -> dict[str, Any]:
        """Delete one agent membership through the managed access layer."""

        requester = self._ensure_requester_principal(user_id)
        _config_path, decision, denied = self._validate_membership_management(
            agent_id=agent_id,
            requester=requester,
            denied_action="delete_membership",
            denied_target_principal_id=str(principal_id or "").strip(),
            denied_details={"source": "client_api"},
        )
        if denied is not None:
            return denied

        normalized_principal_id = str(principal_id or "").strip()
        if not normalized_principal_id:
            return _error("INVALID_REQUEST", "Field 'principal_id' is required.")
        previous_membership = self._agent_access_store.get_membership(
            agent_id=agent_id,
            principal_id=normalized_principal_id,
        )

        deleted = self._agent_access_store.delete_membership(
            agent_id=agent_id,
            principal_id=normalized_principal_id,
        )
        self._agent_access_store.record_audit(
            agent_id=agent_id,
            actor_principal_id=requester.principal_id,
            actor_relation=decision.relation_to_agent,
            action="delete_membership",
            target_principal_id=normalized_principal_id,
            details={
                "allowed": True,
                "reason": decision.reason,
                "deleted": deleted,
                "previous_relation": previous_membership.relation if previous_membership is not None else None,
                "source": "client_api",
            },
        )
        self._invalidate_agent_access_caches(agent_id)
        return _ok({"deleted": deleted, "principal_id": normalized_principal_id})

    def batch_add_participants(
        self,
        agent_id: str,
        principal_ids: list[str] | tuple[str, ...],
        *,
        user_id: str = "ppx-client-user",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Add multiple participant memberships in one managed operation."""
        return self._batch_manage_participants(
            agent_id=agent_id,
            principal_ids=principal_ids,
            operation="add",
            user_id=user_id,
            dry_run=dry_run,
        )

    def batch_remove_participants(
        self,
        agent_id: str,
        principal_ids: list[str] | tuple[str, ...],
        *,
        user_id: str = "ppx-client-user",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Remove multiple participant memberships in one managed operation."""
        return self._batch_manage_participants(
            agent_id=agent_id,
            principal_ids=principal_ids,
            operation="remove",
            user_id=user_id,
            dry_run=dry_run,
        )

    def sync_participants(
        self,
        agent_id: str,
        principal_ids: list[str] | tuple[str, ...],
        *,
        user_id: str = "ppx-client-user",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Synchronize participant memberships to exactly the requested set."""
        return self._batch_manage_participants(
            agent_id=agent_id,
            principal_ids=principal_ids,
            operation="sync",
            user_id=user_id,
            dry_run=dry_run,
        )

    def _batch_manage_participants(
        self,
        *,
        agent_id: str,
        principal_ids: list[str] | tuple[str, ...],
        operation: str,
        user_id: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Apply one batch participant management operation with one summary audit row."""
        requester = self._ensure_requester_principal(user_id)
        normalized_principal_ids = _normalize_principal_id_list(principal_ids)
        if not normalized_principal_ids:
            return _error("INVALID_REQUEST", "Field 'principal_ids' must contain at least one principal id.")

        action_name = {
            "add": "batch_add_participants",
            "remove": "batch_remove_participants",
            "sync": "sync_participants",
        }.get(operation, "")
        if not action_name:
            return _error("INVALID_REQUEST", f"Unsupported batch operation '{operation}'.")

        _config_path, decision, denied = self._validate_membership_management(
            agent_id=agent_id,
            requester=requester,
            denied_action=action_name,
            denied_details={
                "source": "client_api",
                "dry_run": bool(dry_run),
                "requested_principal_ids": normalized_principal_ids,
            },
        )
        if denied is not None:
            return denied

        current_memberships = self._agent_access_store.list_memberships(
            agent_id=agent_id,
            relations=("participant",),
        )
        current_ids = {membership.principal_id for membership in current_memberships}
        requested_ids = set(normalized_principal_ids)

        if operation == "add":
            added_ids = [principal_id for principal_id in normalized_principal_ids if principal_id not in current_ids]
            removed_ids: list[str] = []
            unchanged_ids = [principal_id for principal_id in normalized_principal_ids if principal_id in current_ids]
        elif operation == "remove":
            added_ids = []
            removed_ids = [principal_id for principal_id in normalized_principal_ids if principal_id in current_ids]
            unchanged_ids = [principal_id for principal_id in normalized_principal_ids if principal_id not in current_ids]
        else:
            added_ids = [principal_id for principal_id in normalized_principal_ids if principal_id not in current_ids]
            removed_ids = sorted(principal_id for principal_id in current_ids if principal_id not in requested_ids)
            unchanged_ids = [principal_id for principal_id in normalized_principal_ids if principal_id in current_ids]

        if not dry_run:
            for principal_id in added_ids:
                principal = ensure_access_principal(
                    self._identity_store,
                    principal_id=principal_id,
                    source="client_api_access_mutation",
                    account_kind="managed_access",
                )
                if principal is None:
                    return _error("RUNTIME_UNAVAILABLE", f"Could not ensure principal '{principal_id}'.")
                self._agent_access_store.upsert_membership(
                    AgentMembership(
                        agent_id=agent_id,
                        principal_id=principal.principal_id,
                        relation="participant",
                        metadata={"source": "client_api_batch"},
                    )
                )
            for principal_id in removed_ids:
                self._agent_access_store.delete_membership(agent_id=agent_id, principal_id=principal_id)
            if added_ids or removed_ids:
                self._invalidate_agent_access_caches(agent_id)

        audit_details = {
            "allowed": True,
            "reason": decision.reason,
            "source": "client_api",
            "dry_run": bool(dry_run),
            "applied": not dry_run,
            "requested_principal_ids": normalized_principal_ids,
            "added_principal_ids": added_ids,
            "removed_principal_ids": removed_ids,
            "unchanged_principal_ids": unchanged_ids,
            "requested_count": len(normalized_principal_ids),
            "added_count": len(added_ids),
            "removed_count": len(removed_ids),
            "unchanged_count": len(unchanged_ids),
        }
        self._record_admin_audit(
            agent_id=agent_id,
            requester=requester,
            action=action_name,
            relation_to_agent=decision.relation_to_agent,
            details=audit_details,
        )
        return _ok(
            {
                "operation": action_name,
                "dry_run": bool(dry_run),
                "applied": not dry_run,
                "requested_principal_ids": normalized_principal_ids,
                "added_principal_ids": added_ids,
                "removed_principal_ids": removed_ids,
                "unchanged_principal_ids": unchanged_ids,
                "summary": {
                    "requested_count": len(normalized_principal_ids),
                    "added_count": len(added_ids),
                    "removed_count": len(removed_ids),
                    "unchanged_count": len(unchanged_ids),
                },
            }
        )

    def create_run(self, agent_id: str, session_id: str, text: str, *, user_id: str = "ppx-client-user") -> dict[str, Any]:
        """Create one streaming run and start consuming worker events in background."""

        requester = self._ensure_requester_principal(user_id)
        config_path = self._ensure_agent_access_state(agent_id)
        if config_path is None:
            return _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        location = self._find_session_owner(
            session_id=session_id,
            requester_principal_id=requester.principal_id,
        )
        if isinstance(location, dict):
            error_code = location.get("error", {}).get("code")
            if error_code == "SESSION_NOT_FOUND":
                located_agent_id = agent_id
                subject_principal_id = requester.principal_id
            else:
                return location
        else:
            located_agent_id, subject_principal_id = location
            if located_agent_id != agent_id:
                return _error("SESSION_NOT_FOUND", f"Session '{session_id}' was not found for agent '{agent_id}'.")
            if subject_principal_id != requester.principal_id:
                return _error(
                    "ACCESS_DENIED",
                    f"Principal '{requester.principal_id}' cannot start a run in session '{session_id}'.",
                    {"reason": "run_requires_session_owner"},
                )
        run_id = f"run_{os.urandom(8).hex()}"
        cmd = [
            sys.executable,
            "-m",
            "openppx.runtime.client_api_worker",
            "run",
            "--config-path",
            str(config_path),
            "--session-id",
            session_id,
            "--message",
            text,
            "--user-id",
            requester.principal_id,
        ]
        process = subprocess.Popen(
            cmd,
            cwd=str(config_path.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        handle = RunHandle(run_id=run_id, agent_id=agent_id, session_id=session_id, process=process)
        with self._lock:
            self._runs[run_id] = handle
        self._session_agents[session_id] = agent_id
        self._session_owners[session_id] = requester.principal_id
        self._invalidate_agent_cache(agent_id, user_id=requester.principal_id)
        self._invalidate_session_cache(session_id, user_id=requester.principal_id)
        _debug(
            "client_api.create_run",
            {
                "run_id": run_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "user_id": requester.principal_id,
                "text_preview": text[:240] + ("..." if len(text) > 240 else ""),
                "worker_cmd": cmd,
            },
        )
        handle.publish(
            "run.started",
            {
                "run_id": run_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "created_at": _iso_now(),
            },
        )
        handle.publish(
            "message.created",
            {
                "run_id": run_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "message_id": handle.assistant_message_id,
                "message": _message_payload(
                    message_id=handle.assistant_message_id,
                    session_id=session_id,
                    role="assistant",
                    parts=[],
                    status="streaming",
                ),
            },
        )
        thread = threading.Thread(
            target=self._consume_run_process,
            args=(handle,),
            daemon=True,
        )
        thread.start()
        stderr_thread = threading.Thread(
            target=self._consume_run_stderr,
            args=(handle,),
            daemon=True,
        )
        stderr_thread.start()
        return _ok(
            {
                "run": {
                    "id": run_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "status": "running",
                    "events_url": f"/api/v1/runs/{run_id}/events",
                }
            }
        )

    def search_memory(self, agent_id: str, query: str, *, user_id: str = "ppx-client-user") -> dict[str, Any]:
        """Run one explicit memory query through the access-controlled query layer."""
        requester = self._ensure_requester_principal(user_id)
        if self._ensure_agent_access_state(agent_id) is None:
            return _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        try:
            result = asyncio.run(
                self._memory_query_service.search(
                    agent_id=agent_id,
                    requester_principal_id=requester.principal_id,
                    query=query,
                )
            )
        except Exception as exc:
            return _error("RUNTIME_UNAVAILABLE", str(exc))
        if not result.decision.allow:
            return _error(
                "ACCESS_DENIED",
                f"Principal '{requester.principal_id}' cannot query memory for agent '{agent_id}'.",
                {"reason": result.decision.reason},
            )
        return _ok(
            {
                "items": [
                    {
                        "id": memory.id,
                        "author": memory.author,
                        "timestamp": memory.timestamp,
                        "text": memory_entry_text(memory),
                        "subject_principal_id": memory.custom_metadata.get("subject_principal_id"),
                        "metadata": dict(memory.custom_metadata),
                    }
                    for memory in result.memories
                ]
            }
        )

    def get_memory_audit(
        self,
        agent_id: str,
        *,
        user_id: str = "ppx-client-user",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return visible explicit-memory audit rows for one requester and agent."""
        requester = self._ensure_requester_principal(user_id)
        if self._ensure_agent_access_state(agent_id) is None:
            return _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        try:
            result = self._memory_query_service.list_audit(
                agent_id=agent_id,
                requester_principal_id=requester.principal_id,
                limit=limit,
            )
        except Exception as exc:
            return _error("RUNTIME_UNAVAILABLE", str(exc))
        if not result.decision.allow:
            self._record_admin_audit(
                agent_id=agent_id,
                requester=requester,
                action="read_memory_audit",
                relation_to_agent=result.decision.relation_to_agent,
                details={"allowed": False, "reason": result.decision.reason, "limit": limit},
            )
            return _error(
                "ACCESS_DENIED",
                f"Principal '{requester.principal_id}' cannot read memory audit for agent '{agent_id}'.",
                {"reason": result.decision.reason},
            )
        payload = _ok(
            {
                "items": result.rows,
                "requester": {
                    "principal_id": requester.principal_id,
                    "relation": result.decision.relation_to_agent,
                    "reason": result.decision.reason,
                    "scope_kind": result.decision.scope_kind,
                },
            }
        )
        self._record_admin_audit(
            agent_id=agent_id,
            requester=requester,
            action="read_memory_audit",
            relation_to_agent=result.decision.relation_to_agent,
            details={
                "allowed": True,
                "reason": result.decision.reason,
                "limit": limit,
                "result_count": len(result.rows),
            },
        )
        return payload

    def get_access_audit(
        self,
        agent_id: str,
        *,
        user_id: str = "ppx-client-user",
        limit: int = 50,
        category: str = "all",
    ) -> dict[str, Any]:
        """Return visible admin-audit rows for one requester and agent."""
        requester = self._ensure_requester_principal(user_id)
        if self._ensure_agent_access_state(agent_id) is None:
            return _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        try:
            normalized_category = _normalize_access_audit_category(category)
        except ValueError as exc:
            return _error("INVALID_REQUEST", str(exc))
        decision = self._access_policy.decide_agent_management(
            requester_principal_id=requester.principal_id,
            agent_id=agent_id,
            access_kind="access_audit_read",
        )
        if not decision.allow:
            self._record_admin_audit(
                agent_id=agent_id,
                requester=requester,
                action="read_admin_audit",
                relation_to_agent=decision.relation_to_agent,
                details={
                    "allowed": False,
                    "reason": decision.reason,
                    "limit": limit,
                    "category": normalized_category,
                },
            )
            return _error(
                "ACCESS_DENIED",
                f"Principal '{requester.principal_id}' cannot read access audit for agent '{agent_id}'.",
                {"reason": decision.reason},
            )
        rows = self._agent_access_store.list_audit(
            agent_id=agent_id,
            limit=limit,
            actions=_actions_for_access_audit_category(normalized_category),
        )
        payload = _ok(
            {
                "items": [
                    {
                        "audit_id": row.audit_id,
                        "agent_id": row.agent_id,
                        "actor_principal_id": row.actor_principal_id,
                        "actor_relation": row.actor_relation,
                        "action": row.action,
                        "target_principal_id": row.target_principal_id,
                        "details": dict(row.details),
                        "created_at_ms": row.created_at_ms,
                    }
                    for row in rows
                ],
                "requester": {
                    "principal_id": requester.principal_id,
                    "relation": decision.relation_to_agent,
                    "reason": decision.reason,
                    "scope_kind": decision.scope_kind,
                },
                "category": normalized_category,
            }
        )
        self._record_admin_audit(
            agent_id=agent_id,
            requester=requester,
            action="read_admin_audit",
            relation_to_agent=decision.relation_to_agent,
            details={
                "allowed": True,
                "reason": decision.reason,
                "limit": limit,
                "category": normalized_category,
                "result_count": len(rows),
            },
        )
        return payload

    def _consume_run_stderr(self, handle: RunHandle) -> None:
        """Continuously collect worker stderr for debug visibility."""

        assert handle.process.stderr is not None
        for raw_line in handle.process.stderr:
            line = raw_line.strip()
            if not line:
                continue
            handle.append_stderr_line(line)
            _debug(
                "client_api.worker.stderr",
                {
                    "run_id": handle.run_id,
                    "line_preview": line[:400] + ("..." if len(line) > 400 else ""),
                },
            )

    def _consume_run_process(self, handle: RunHandle) -> None:
        """Translate worker NDJSON lines into replayable SSE events."""

        assert handle.process.stdout is not None
        final_text = ""

        def _publish_run_failure(error_message: str, *, code: str = "RUN_FAILED") -> None:
            handle.failed = True
            _debug(
                "client_api.message.failed",
                {
                    "run_id": handle.run_id,
                    "message": error_message,
                },
            )
            handle.publish(
                "message.failed",
                {
                    "run_id": handle.run_id,
                    "agent_id": handle.agent_id,
                    "session_id": handle.session_id,
                    "message_id": handle.assistant_message_id,
                    "status": "failed",
                    "error": _error_part_payload(code=code, text=error_message),
                },
            )
            handle.publish(
                "error",
                {
                    "run_id": handle.run_id,
                    "code": code,
                    "message": error_message,
                },
            )

        for line in handle.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                _debug(
                    "client_api.worker.invalid_json",
                    {
                        "run_id": handle.run_id,
                        "line_preview": line[:320],
                    },
                )
                continue
            event_type = str(payload.get("type") or "")
            _debug(
                "client_api.worker.payload",
                {
                    "run_id": handle.run_id,
                    "event_type": event_type or "unknown",
                    "keys": sorted(payload.keys()),
                },
            )
            if event_type == "event":
                event = payload.get("event")
                if isinstance(event, dict):
                    content = event.get("content") if isinstance(event.get("content"), dict) else {}
                    raw_parts = content.get("parts") if isinstance(content, dict) and isinstance(content.get("parts"), list) else []
                    raw_long_running_ids = event.get("long_running_tool_ids") or []
                    long_running_ids = set(str(item) for item in raw_long_running_ids if item is not None)
                    for raw_part in raw_parts:
                        if not isinstance(raw_part, dict):
                            continue
                        function_call = raw_part.get("function_call")
                        if isinstance(function_call, dict):
                            step_id = str(function_call.get("id") or "step")
                            _debug(
                                "client_api.step.updated",
                                {
                                    "run_id": handle.run_id,
                                    "step_id": step_id,
                                    "title": str(function_call.get("name") or "tool"),
                                    "status": "running",
                                    "long_running": step_id in long_running_ids,
                                },
                            )
                            handle.publish(
                                "step.updated",
                                {
                                    "run_id": handle.run_id,
                                    "agent_id": handle.agent_id,
                                    "session_id": handle.session_id,
                                    "message_id": handle.assistant_message_id,
                                    "step": _step_ref_payload(
                                        step_id=step_id,
                                        title=str(function_call.get("name") or "tool"),
                                        status="running",
                                        detail=(
                                            "Background task is running.\n\n" + _preview_value(function_call.get("args"), "No tool arguments")
                                            if step_id in long_running_ids
                                            else _preview_value(function_call.get("args"), "No tool arguments")
                                        ),
                                    ),
                                },
                            )
                        function_response = raw_part.get("function_response")
                        if isinstance(function_response, dict):
                            _debug(
                                "client_api.step.updated",
                                {
                                    "run_id": handle.run_id,
                                    "step_id": str(function_response.get("id") or "step"),
                                    "title": str(function_response.get("name") or "tool"),
                                    "status": "completed",
                                },
                            )
                            handle.publish(
                                "step.updated",
                                {
                                    "run_id": handle.run_id,
                                    "agent_id": handle.agent_id,
                                    "session_id": handle.session_id,
                                    "message_id": handle.assistant_message_id,
                                    "step": _step_ref_payload(
                                        step_id=str(function_response.get("id") or "step"),
                                        title=str(function_response.get("name") or "tool"),
                                        status="completed",
                                        detail=_preview_value(function_response.get("response"), "Tool returned without a payload"),
                                    ),
                                },
                            )
            elif event_type == "delta":
                final_text = str(payload.get("text") or final_text)
                _debug(
                    "client_api.message.delta",
                    {
                        "run_id": handle.run_id,
                        "text_length": len(final_text),
                    },
                )
                handle.publish(
                    "message.delta",
                    {
                        "run_id": handle.run_id,
                        "agent_id": handle.agent_id,
                        "session_id": handle.session_id,
                        "message_id": handle.assistant_message_id,
                        "status": "streaming",
                        "part": {
                            "type": "markdown",
                            "text": final_text,
                        },
                    },
                )
            elif event_type == "final":
                final_text = str(payload.get("text") or final_text)
                if not final_text.strip():
                    _publish_run_failure(
                        "Worker finished without returning a final reply.",
                        code="RUN_EMPTY_FINAL",
                    )
                    continue
                _debug(
                    "client_api.message.completed",
                    {
                        "run_id": handle.run_id,
                        "text_length": len(final_text),
                    },
                )
                handle.publish(
                    "message.completed",
                    {
                        "run_id": handle.run_id,
                        "agent_id": handle.agent_id,
                        "session_id": handle.session_id,
                        "message_id": handle.assistant_message_id,
                        "status": "completed",
                        "message": _message_payload(
                            message_id=handle.assistant_message_id,
                            session_id=handle.session_id,
                            role="assistant",
                            parts=[{"type": "markdown", "text": final_text}],
                            status="completed",
                        ),
                    },
                )
            elif event_type == "error":
                error_message = str(payload.get("message") or "Unknown worker error")
                _publish_run_failure(error_message)
        exit_code = handle.process.wait()
        stderr_text = handle.stderr_text()
        _debug(
            "client_api.worker.exit",
            {
                "run_id": handle.run_id,
                "exit_code": exit_code,
                "failed": handle.failed,
                "stderr_preview": stderr_text[:400] + ("..." if len(stderr_text) > 400 else ""),
            },
        )
        if exit_code != 0 and not handle.failed:
            _publish_run_failure(
                stderr_text or "worker exited unexpectedly",
                code="WORKER_EXIT_ERROR",
            )
        _debug(
            "client_api.run.finished",
            {
                "run_id": handle.run_id,
                "agent_id": handle.agent_id,
                "session_id": handle.session_id,
                "status": "failed" if handle.failed else "completed",
            },
        )
        handle.publish(
            "run.finished",
            {
                "run_id": handle.run_id,
                "agent_id": handle.agent_id,
                "session_id": handle.session_id,
                "message_id": handle.assistant_message_id,
                "status": "failed" if handle.failed else "completed",
            },
        )
        handle.finish()

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Cancel one active run."""

        handle = self._runs.get(run_id)
        if handle is None:
            return _error("RUN_NOT_FOUND", f"Run '{run_id}' was not found.")
        cancelled = handle.cancel()
        if not cancelled:
            return _error("RUN_ALREADY_FINISHED", f"Run '{run_id}' has already finished.")
        _debug("client_api.cancel_run", {"run_id": run_id})
        return _ok({"run": {"id": run_id, "status": "cancelled"}})

    def stream_run_events(self, run_id: str, *, last_event_id: str | None = None) -> queue.Queue[RunEnvelope | None] | None:
        """Return one subscriber queue for SSE streaming."""

        handle = self._runs.get(run_id)
        if handle is None:
            return None
        return handle.subscribe(last_event_id=last_event_id)


class _ClientApiHandler(BaseHTTPRequestHandler):
    """HTTP request handler bound to one coordinator instance."""

    server_version = "OpenPpxClientApi/0.1"

    @property
    def coordinator(self) -> ClientApiCoordinator:
        return self.server.coordinator  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    def _parse(self) -> tuple[str, list[str], dict[str, str]]:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"
        segments = [segment for segment in path.split("/") if segment]
        query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items() if values}
        return path, segments, query

    def do_GET(self) -> None:  # noqa: N802
        path, segments, query = self._parse()
        if path == "/api/v1/health":
            self._send_json(200, self.coordinator.health())
            return
        if path == "/api/v1/agents":
            self._send_json(200, self.coordinator.list_agents())
            return
        if path == "/api/v1/runtime/status":
            self._send_json(200, self.coordinator.runtime_status())
            return
        if len(segments) == 5 and segments[:3] == ["api", "v1", "agents"] and segments[4] == "sessions":
            user_id = str(query.get("user_id") or "ppx-client-user")
            payload = self.coordinator.list_sessions(segments[3], user_id=user_id)
            self._send_json(200 if payload.get("ok") else 404, payload)
            return
        if len(segments) == 5 and segments[:3] == ["api", "v1", "agents"] and segments[4] == "access":
            user_id = str(query.get("user_id") or "ppx-client-user")
            payload = self.coordinator.get_agent_access(segments[3], user_id=user_id)
            self._send_json(200 if payload.get("ok") else 404, payload)
            return
        if len(segments) == 6 and segments[:3] == ["api", "v1", "agents"] and segments[4] == "access" and segments[5] == "audit":
            user_id = str(query.get("user_id") or "ppx-client-user")
            raw_limit = str(query.get("limit") or "50").strip()
            category = str(query.get("category") or "all")
            try:
                limit = int(raw_limit)
            except ValueError:
                self._send_json(400, _error("INVALID_REQUEST", "Query parameter 'limit' must be an integer."))
                return
            payload = self.coordinator.get_access_audit(
                segments[3],
                user_id=user_id,
                limit=limit,
                category=category,
            )
            status = 200 if payload.get("ok") else 403
            if not payload.get("ok") and payload.get("error", {}).get("code") == "AGENT_NOT_FOUND":
                status = 404
            if not payload.get("ok") and payload.get("error", {}).get("code") == "INVALID_REQUEST":
                status = 400
            self._send_json(status, payload)
            return
        if len(segments) == 5 and segments[:3] == ["api", "v1", "sessions"] and segments[4] == "messages":
            user_id = str(query.get("user_id") or "ppx-client-user")
            payload = self.coordinator.get_session_messages(segments[3], user_id=user_id)
            self._send_json(200 if payload.get("ok") else 404, payload)
            return
        if len(segments) == 6 and segments[:3] == ["api", "v1", "agents"] and segments[4] == "memory" and segments[5] == "search":
            query_text = str(query.get("q") or "").strip()
            user_id = str(query.get("user_id") or "ppx-client-user")
            if not query_text:
                self._send_json(400, _error("INVALID_REQUEST", "Query parameter 'q' is required."))
                return
            payload = self.coordinator.search_memory(segments[3], query_text, user_id=user_id)
            self._send_json(200 if payload.get("ok") else 404, payload)
            return
        if len(segments) == 6 and segments[:3] == ["api", "v1", "agents"] and segments[4] == "memory" and segments[5] == "audit":
            user_id = str(query.get("user_id") or "ppx-client-user")
            raw_limit = str(query.get("limit") or "50").strip()
            try:
                limit = int(raw_limit)
            except ValueError:
                self._send_json(400, _error("INVALID_REQUEST", "Query parameter 'limit' must be an integer."))
                return
            payload = self.coordinator.get_memory_audit(segments[3], user_id=user_id, limit=limit)
            self._send_json(200 if payload.get("ok") else 404, payload)
            return
        if len(segments) == 5 and segments[:3] == ["api", "v1", "runs"] and segments[4] == "events":
            run_id = segments[3]
            subscriber = self.coordinator.stream_run_events(run_id, last_event_id=self.headers.get("Last-Event-ID"))
            if subscriber is None:
                self._send_json(404, _error("RUN_NOT_FOUND", f"Run '{run_id}' was not found."))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            while True:
                item = subscriber.get()
                if item is None:
                    break
                self.wfile.write(f"id: {item.event_id}\n".encode("utf-8"))
                self.wfile.write(f"event: {item.event}\n".encode("utf-8"))
                self.wfile.write(f"data: {json.dumps(item.payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            return
        self._send_json(404, _error("NOT_FOUND", f"Unknown path: {path}"))

    def do_POST(self) -> None:  # noqa: N802
        path, segments, _query = self._parse()
        body = self._read_json_body()
        if len(segments) == 6 and segments[:3] == ["api", "v1", "agents"] and segments[4] == "access" and segments[5] == "owner":
            user_id = str(body.get("user_id") or "ppx-client-user")
            owner_principal_id = str(body.get("owner_principal_id") or "").strip()
            if not owner_principal_id:
                self._send_json(400, _error("INVALID_REQUEST", "Field 'owner_principal_id' is required."))
                return
            payload = self.coordinator.set_agent_owner(segments[3], owner_principal_id, user_id=user_id)
            self._send_json(200 if payload.get("ok") else 403, payload)
            return
        if len(segments) == 6 and segments[:3] == ["api", "v1", "agents"] and segments[4] == "access" and segments[5] == "memberships":
            user_id = str(body.get("user_id") or "ppx-client-user")
            principal_id = str(body.get("principal_id") or "").strip()
            relation = str(body.get("relation") or "participant").strip()
            if not principal_id:
                self._send_json(400, _error("INVALID_REQUEST", "Field 'principal_id' is required."))
                return
            payload = self.coordinator.upsert_agent_membership(
                segments[3],
                principal_id,
                relation=relation,
                user_id=user_id,
            )
            self._send_json(200 if payload.get("ok") else 403, payload)
            return
        if len(segments) == 7 and segments[:3] == ["api", "v1", "agents"] and segments[4] == "access" and segments[5] == "memberships" and segments[6] == "batch":
            user_id = str(body.get("user_id") or "ppx-client-user")
            operation = str(body.get("operation") or "").strip().lower()
            dry_run = bool(body.get("dry_run"))
            raw_principal_ids = body.get("principal_ids")
            if not isinstance(raw_principal_ids, list):
                self._send_json(400, _error("INVALID_REQUEST", "Field 'principal_ids' must be a JSON array."))
                return
            principal_ids = [str(item or "") for item in raw_principal_ids]
            if operation == "add":
                payload = self.coordinator.batch_add_participants(
                    segments[3],
                    principal_ids,
                    user_id=user_id,
                    dry_run=dry_run,
                )
            elif operation == "remove":
                payload = self.coordinator.batch_remove_participants(
                    segments[3],
                    principal_ids,
                    user_id=user_id,
                    dry_run=dry_run,
                )
            elif operation == "sync":
                payload = self.coordinator.sync_participants(
                    segments[3],
                    principal_ids,
                    user_id=user_id,
                    dry_run=dry_run,
                )
            else:
                self._send_json(400, _error("INVALID_REQUEST", "Field 'operation' must be add, remove, or sync."))
                return
            status = 200 if payload.get("ok") else 403
            if not payload.get("ok") and payload.get("error", {}).get("code") == "AGENT_NOT_FOUND":
                status = 404
            if not payload.get("ok") and payload.get("error", {}).get("code") == "INVALID_REQUEST":
                status = 400
            self._send_json(status, payload)
            return
        if len(segments) == 5 and segments[:3] == ["api", "v1", "agents"] and segments[4] == "sessions":
            user_id = str(body.get("user_id") or "ppx-client-user")
            payload = self.coordinator.create_session(segments[3], user_id=user_id)
            self._send_json(200 if payload.get("ok") else 404, payload)
            return
        if len(segments) == 7 and segments[:3] == ["api", "v1", "agents"] and segments[4] == "sessions" and segments[6] == "runs":
            text = str(body.get("text") or "").strip()
            user_id = str(body.get("user_id") or "ppx-client-user")
            if not text:
                self._send_json(400, _error("INVALID_REQUEST", "Field 'text' is required."))
                return
            payload = self.coordinator.create_run(segments[3], segments[5], text, user_id=user_id)
            self._send_json(200 if payload.get("ok") else 404, payload)
            return
        if len(segments) == 5 and segments[:3] == ["api", "v1", "runs"] and segments[4] == "cancel":
            payload = self.coordinator.cancel_run(segments[3])
            self._send_json(200 if payload.get("ok") else 404, payload)
            return
        self._send_json(404, _error("NOT_FOUND", f"Unknown path: {path}"))

    def do_DELETE(self) -> None:  # noqa: N802
        path, segments, query = self._parse()
        if len(segments) == 7 and segments[:3] == ["api", "v1", "agents"] and segments[4] == "access" and segments[5] == "memberships":
            user_id = str(query.get("user_id") or "ppx-client-user")
            payload = self.coordinator.delete_agent_membership(
                segments[3],
                segments[6],
                user_id=user_id,
            )
            self._send_json(200 if payload.get("ok") else 403, payload)
            return
        self._send_json(404, _error("NOT_FOUND", f"Unknown path: {path}"))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        """Silence default stdlib access logs for cleaner CLI output."""


class ClientApiHttpServer(ThreadingHTTPServer):
    """Threading HTTP server bound to one `ClientApiCoordinator`."""

    def __init__(self, server_address: tuple[str, int], coordinator: ClientApiCoordinator) -> None:
        super().__init__(server_address, _ClientApiHandler)
        self.coordinator = coordinator


def serve_client_api(*, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Start the local client API HTTP server."""

    coordinator = ClientApiCoordinator()
    server = ClientApiHttpServer((host, port), coordinator)
    print(f"openppx client-api listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
