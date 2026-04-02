"""Local HTTP + SSE client API service for openpipixia."""

from __future__ import annotations

import datetime as dt
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
    description = f"Workspace: {workspace}" if workspace else "Local openpipixia agent"
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

    cmd = [sys.executable, "-m", "openpipixia.runtime.client_api_worker", *args]
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


def project_session_event(event: dict[str, Any], session_id: str) -> dict[str, Any]:
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
        text = raw_part.get("text")
        if isinstance(text, str) and text.strip():
            parts.append({"type": "markdown", "text": text})
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
            parts.append(
                {
                    "type": "step_ref",
                    "step_id": step_id,
                    "title": str(function_response.get("name") or "tool"),
                    "status": "completed",
                    "detail": _preview_value(function_response.get("response"), "Tool returned without a payload"),
                }
            )
            parts.append(
                {
                    "type": "code",
                    "language": "json",
                    "text": json.dumps(function_response.get("response") or {}, ensure_ascii=False, indent=2),
                }
            )
    if not parts:
        parts = [{"type": "markdown", "text": "(event without renderable text)"}]
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

    def cancel(self) -> bool:
        """Terminate the subprocess if it is still running."""

        if self.done.is_set():
            return False
        if self.process.poll() is None:
            self.process.terminate()
        self.publish(
            "run.cancelled",
            {
                "run_id": self.run_id,
                "agent_id": self.agent_id,
                "session_id": self.session_id,
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

    def __init__(self, *, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or get_data_dir()
        self._session_agents: dict[str, str] = {}
        self._runs: dict[str, RunHandle] = {}
        self._lock = threading.Lock()

    def health(self) -> dict[str, Any]:
        """Return a lightweight health payload."""

        return _ok(
            {
                "service": "openpipixia-client-api",
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

    def list_sessions(self, agent_id: str) -> dict[str, Any]:
        """Return projected session summaries for one agent."""

        config_path = agent_config_path(agent_id, self.data_dir)
        if not config_path.exists():
            return _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        try:
            response = _run_worker_command(
                config_path=config_path,
                args=["list_sessions", "--config-path", str(config_path)],
            )
        except Exception as exc:
            return _error("RUNTIME_UNAVAILABLE", str(exc))
        items = []
        for session in response.get("sessions", []):
            if not isinstance(session, dict):
                continue
            session_id = str(session.get("id") or "")
            if not session_id:
                continue
            self._session_agents[session_id] = agent_id
            updated_raw = session.get("last_update_time")
            if isinstance(updated_raw, (int, float)):
                updated_at = dt.datetime.fromtimestamp(updated_raw, tz=dt.timezone.utc).astimezone().isoformat()
            else:
                updated_at = _iso_now()
            items.append(
                {
                    "id": session_id,
                    "agent_id": agent_id,
                    "title": f"Session {session_id[:8]}",
                    "updated_at": updated_at,
                    "last_message_preview": str(session.get("last_preview") or "Openpipixia session"),
                    "archived": False,
                }
            )
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        return _ok({"items": items})

    def create_session(self, agent_id: str) -> dict[str, Any]:
        """Create one session for the target agent."""

        config_path = agent_config_path(agent_id, self.data_dir)
        if not config_path.exists():
            return _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        session_id = f"{agent_id}-{os.urandom(8).hex()}"
        try:
            response = _run_worker_command(
                config_path=config_path,
                args=["create_session", "--config-path", str(config_path), "--session-id", session_id],
            )
        except Exception as exc:
            return _error("RUNTIME_UNAVAILABLE", str(exc))
        session = response.get("session") if isinstance(response.get("session"), dict) else {}
        session_id = str(session.get("id") or session_id)
        self._session_agents[session_id] = agent_id
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
                    "title": "New local session",
                    "updated_at": updated_at,
                    "last_message_preview": "Start a task for this agent.",
                    "archived": False,
                }
            }
        )

    def get_session_messages(self, session_id: str) -> dict[str, Any]:
        """Return projected message history for one session."""

        agent_id = self._session_agents.get(session_id)
        if not agent_id:
            for candidate in list_enabled_agent_names(self.data_dir):
                sessions = self.list_sessions(candidate)
                if not sessions.get("ok"):
                    continue
                items = sessions["data"].get("items", [])
                if any(item.get("id") == session_id for item in items):
                    agent_id = candidate
                    self._session_agents[session_id] = candidate
                    break
        if not agent_id:
            return _error("SESSION_NOT_FOUND", f"Session '{session_id}' was not found.")
        config_path = agent_config_path(agent_id, self.data_dir)
        try:
            response = _run_worker_command(
                config_path=config_path,
                args=["get_session", "--config-path", str(config_path), "--session-id", session_id],
            )
        except Exception as exc:
            return _error("RUNTIME_UNAVAILABLE", str(exc))
        session = response.get("session") if isinstance(response.get("session"), dict) else None
        if session is None:
            return _error("SESSION_NOT_FOUND", f"Session '{session_id}' was not found.")
        events = session.get("events") if isinstance(session.get("events"), list) else []
        messages = [project_session_event(event, session_id) for event in events if isinstance(event, dict)]
        return _ok({"items": messages})

    def create_run(self, agent_id: str, session_id: str, text: str, *, user_id: str = "ppx-client-user") -> dict[str, Any]:
        """Create one streaming run and start consuming worker events in background."""

        config_path = agent_config_path(agent_id, self.data_dir)
        if not config_path.exists():
            return _error("AGENT_NOT_FOUND", f"Agent '{agent_id}' was not found.")
        run_id = f"run_{os.urandom(8).hex()}"
        cmd = [
            sys.executable,
            "-m",
            "openpipixia.runtime.client_api_worker",
            "run",
            "--config-path",
            str(config_path),
            "--session-id",
            session_id,
            "--message",
            text,
            "--user-id",
            user_id,
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
                "message": {
                    "id": handle.assistant_message_id,
                    "session_id": session_id,
                    "role": "assistant",
                    "parts": [],
                    "status": "streaming",
                    "created_at": _iso_now(),
                    "metadata": {},
                },
            },
        )
        thread = threading.Thread(
            target=self._consume_run_process,
            args=(handle,),
            daemon=True,
        )
        thread.start()
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

    def _consume_run_process(self, handle: RunHandle) -> None:
        """Translate worker NDJSON lines into replayable SSE events."""

        assert handle.process.stdout is not None
        final_text = ""
        for line in handle.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            event_type = str(payload.get("type") or "")
            if event_type == "event":
                event = payload.get("event")
                if isinstance(event, dict):
                    content = event.get("content") if isinstance(event.get("content"), dict) else {}
                    raw_parts = content.get("parts") if isinstance(content, dict) and isinstance(content.get("parts"), list) else []
                    long_running_ids = set(str(item) for item in event.get("long_running_tool_ids", []) if item is not None)
                    for raw_part in raw_parts:
                        if not isinstance(raw_part, dict):
                            continue
                        function_call = raw_part.get("function_call")
                        if isinstance(function_call, dict):
                            step_id = str(function_call.get("id") or "step")
                            handle.publish(
                                "step.updated",
                                {
                                    "run_id": handle.run_id,
                                    "message_id": handle.assistant_message_id,
                                    "step": {
                                        "step_id": step_id,
                                        "title": str(function_call.get("name") or "tool"),
                                        "status": "running",
                                        "detail": (
                                            "Background task is running.\n\n" + _preview_value(function_call.get("args"), "No tool arguments")
                                            if step_id in long_running_ids
                                            else _preview_value(function_call.get("args"), "No tool arguments")
                                        ),
                                    },
                                },
                            )
                        function_response = raw_part.get("function_response")
                        if isinstance(function_response, dict):
                            handle.publish(
                                "step.updated",
                                {
                                    "run_id": handle.run_id,
                                    "message_id": handle.assistant_message_id,
                                    "step": {
                                        "step_id": str(function_response.get("id") or "step"),
                                        "title": str(function_response.get("name") or "tool"),
                                        "status": "completed",
                                        "detail": _preview_value(function_response.get("response"), "Tool returned without a payload"),
                                    },
                                },
                            )
            elif event_type == "delta":
                final_text = str(payload.get("text") or final_text)
                handle.publish(
                    "message.delta",
                    {
                        "run_id": handle.run_id,
                        "message_id": handle.assistant_message_id,
                        "part": {
                            "type": "markdown",
                            "text": final_text,
                        },
                    },
                )
            elif event_type == "final":
                final_text = str(payload.get("text") or final_text)
                handle.publish(
                    "message.completed",
                    {
                        "run_id": handle.run_id,
                        "message_id": handle.assistant_message_id,
                        "message": {
                            "id": handle.assistant_message_id,
                            "session_id": handle.session_id,
                            "role": "assistant",
                            "parts": [{"type": "markdown", "text": final_text}],
                            "status": "completed",
                            "created_at": _iso_now(),
                            "metadata": {},
                        },
                    },
                )
            elif event_type == "error":
                handle.failed = True
                error_message = str(payload.get("message") or "Unknown worker error")
                handle.publish(
                    "message.failed",
                    {
                        "run_id": handle.run_id,
                        "message_id": handle.assistant_message_id,
                        "error": {
                            "type": "error",
                            "error_code": "RUN_FAILED",
                            "text": error_message,
                        },
                    },
                )
                handle.publish(
                    "error",
                    {
                        "run_id": handle.run_id,
                        "code": "RUN_FAILED",
                        "message": error_message,
                    },
                )
        if handle.process.stderr is not None:
            stderr_text = handle.process.stderr.read().strip()
        else:
            stderr_text = ""
        if handle.process.wait() != 0 and not handle.failed:
            handle.publish(
                "error",
                {
                    "run_id": handle.run_id,
                    "code": "WORKER_EXIT_ERROR",
                    "message": stderr_text or "worker exited unexpectedly",
                },
            )
        handle.publish(
            "run.finished",
            {
                "run_id": handle.run_id,
                "agent_id": handle.agent_id,
                "session_id": handle.session_id,
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
            self._send_json(200, self.coordinator.list_sessions(segments[3]))
            return
        if len(segments) == 5 and segments[:3] == ["api", "v1", "sessions"] and segments[4] == "messages":
            payload = self.coordinator.get_session_messages(segments[3])
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
        if len(segments) == 5 and segments[:3] == ["api", "v1", "agents"] and segments[4] == "sessions":
            payload = self.coordinator.create_session(segments[3])
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
    print(f"openpipixia client-api listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
