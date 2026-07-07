from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path

from google.adk.events.event import Event
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from openppx.runtime.access_policy import AccessPolicy
from openppx.runtime.agent_access_store import AgentAccessStore, AgentMembership
from openppx.runtime.client_api_service import (
    ClientApiCoordinator,
    build_agent_profile,
    list_enabled_agent_names,
    project_session_event,
)
from openppx.runtime.identity_models import ResolvedPrincipal
from openppx.runtime.identity_store import IdentityStore
from openppx.runtime.memory_query_service import MemoryQueryService
from openppx.runtime.session_service import SessionConfig, create_session_service
from openppx.runtime.sqlite_memory_service import SQLiteMemoryService


class _FakeProcess:
    def __init__(self, stdout_text: str, stderr_text: str = "", returncode: int = 0) -> None:
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self._returncode = returncode
        self.terminated = False

    def poll(self) -> int | None:
        return None if not self.terminated else self._returncode

    def terminate(self) -> None:
        self.terminated = True

    def wait(self) -> int:
        self.terminated = True
        return self._returncode


class _PendingProcess:
    def __init__(self) -> None:
        self.stdout = self
        self.stderr = io.StringIO("")
        self.terminated = False

    def __iter__(self) -> "_PendingProcess":
        return self

    def __next__(self) -> str:
        while not self.terminated:
            time.sleep(0.01)
        raise StopIteration

    def poll(self) -> int | None:
        return None if not self.terminated else 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self) -> int:
        self.terminated = True
        return 0


def _principal(*, principal_id: str, privilege_level: str = "minimal") -> ResolvedPrincipal:
    return ResolvedPrincipal(
        principal_id=principal_id,
        principal_type="human",
        privilege_level=privilege_level,
        account_kind="local",
        display_name=principal_id,
        authenticated=True,
    )


def _memory(text: str, *, timestamp: str) -> MemoryEntry:
    return MemoryEntry(
        id=f"mem-{abs(hash((text, timestamp)))}",
        author="user",
        timestamp=timestamp,
        content=types.Content(role="user", parts=[types.Part.from_text(text=text)]),
    )


def test_list_enabled_agent_names_reads_global_config(tmp_path: Path) -> None:
    (tmp_path / "writer").mkdir()
    (tmp_path / "reviewer").mkdir()
    (tmp_path / "global_config.json").write_text(
        json.dumps(
            {
                "agents": [
                    {"name": "writer", "enabled": True},
                    {"name": "reviewer", "enabled": False},
                    {"name": "operator", "enabled": True},
                ]
            }
        ),
        encoding="utf-8",
    )

    names = list_enabled_agent_names(tmp_path)
    assert names == ["writer", "operator"]


def test_build_agent_profile_uses_workspace_description(tmp_path: Path) -> None:
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps({"agent": {"workspace": "workspace/writer"}}),
        encoding="utf-8",
    )

    profile = build_agent_profile("writer", tmp_path)
    assert profile["id"] == "writer"
    assert profile["workspace"] == "workspace/writer"
    assert "Workspace:" in profile["description"]


def test_project_session_event_builds_structured_parts() -> None:
    message = project_session_event(
        {
            "id": "evt_1",
            "author": "assistant",
            "timestamp": 1_717_171_717,
            "content": {
                "parts": [
                    {"text": "I will inspect the repo."},
                    {"function_call": {"id": "call_1", "name": "inspect_repo", "args": {"path": "."}}},
                    {"function_response": {"id": "call_1", "name": "inspect_repo", "response": {"ok": True}}},
                ]
            },
        },
        "session_1",
    )

    assert message["role"] == "assistant"
    assert message["parts"][0]["type"] == "markdown"
    assert message["parts"][1]["type"] == "step_ref"
    assert message["parts"][2]["type"] == "step_ref"
    assert message["parts"][3]["type"] == "tool_result"


def test_project_session_event_skips_thought_text() -> None:
    message = project_session_event(
        {
            "id": "evt_thought",
            "author": "assistant",
            "timestamp": 1_717_171_717,
            "content": {
                "parts": [
                    {"text": "hidden reasoning", "thought": True},
                    {"text": "visible answer"},
                ]
            },
        },
        "session_thought",
    )

    assert message is not None
    assert message["parts"] == [{"type": "markdown", "text": "visible answer"}]


def test_project_session_event_skips_unrenderable_events() -> None:
    message = project_session_event(
        {
            "id": "evt_2",
            "author": "system",
            "timestamp": 1_717_171_718,
            "content": {"parts": [{}]},
        },
        "session_2",
    )

    assert message is None


def test_project_session_event_strips_request_time_prefix_from_user_text() -> None:
    message = project_session_event(
        {
            "id": "evt_request_time",
            "author": "user",
            "timestamp": 1_717_171_719,
            "content": {
                "parts": [
                    {
                        "text": (
                            "Current request time: 2026-04-03T12:32:17+08:00 (CST)\n"
                            "Use this as the reference 'now' for relative time expressions in this message.\n\n"
                            "今天日期给我一下"
                        )
                    }
                ]
            },
        },
        "session_request_time",
    )

    assert message is not None
    assert message["role"] == "user"
    assert message["parts"] == [{"type": "markdown", "text": "今天日期给我一下"}]


def test_create_run_streams_replayable_events(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(json.dumps({"agent": {"workspace": "workspace/writer"}}), encoding="utf-8")

    stdout_lines = "\n".join(
        [
            json.dumps(
                {
                    "type": "event",
                    "event": {
                        "content": {
                            "parts": [
                                {"function_call": {"id": "call_1", "name": "inspect_repo", "args": {"path": "."}}},
                            ]
                        }
                    },
                }
            ),
            json.dumps({"type": "delta", "text": "hello"}),
            json.dumps({"type": "final", "text": "hello world"}),
        ]
    )

    monkeypatch.setattr(
        "openppx.runtime.client_api_service.subprocess.Popen",
        lambda *args, **kwargs: _FakeProcess(stdout_lines),
    )

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    payload = coordinator.create_run("writer", "session_1", "hi")
    assert payload["ok"] is True
    run_id = payload["data"]["run"]["id"]

    handle = coordinator._runs[run_id]
    assert handle.done.wait(timeout=1.0)

    subscriber = coordinator.stream_run_events(run_id)
    assert subscriber is not None

    events: list[str] = []
    while True:
        item = subscriber.get(timeout=1.0)
        if item is None:
            break
        events.append(item.event)

    assert "run.started" in events
    assert "message.created" in events
    assert "step.updated" in events
    assert "message.delta" in events
    assert "message.completed" in events
    assert "run.finished" in events


def test_create_run_treats_empty_final_as_failed_message(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(json.dumps({"agent": {"workspace": "workspace/writer"}}), encoding="utf-8")

    stdout_lines = json.dumps({"type": "final", "text": ""})

    monkeypatch.setattr(
        "openppx.runtime.client_api_service.subprocess.Popen",
        lambda *args, **kwargs: _FakeProcess(stdout_lines),
    )

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    payload = coordinator.create_run("writer", "session_empty_final", "hi")
    assert payload["ok"] is True

    run_id = payload["data"]["run"]["id"]
    handle = coordinator._runs[run_id]
    assert handle.done.wait(timeout=1.0)

    subscriber = coordinator.stream_run_events(run_id)
    assert subscriber is not None

    events = []
    while True:
        item = subscriber.get(timeout=1.0)
        if item is None:
            break
        events.append((item.event, item.payload))

    by_event = {name: payload for name, payload in events}
    assert "message.completed" not in by_event
    assert by_event["message.failed"]["status"] == "failed"
    assert by_event["message.failed"]["error"]["text"]
    assert by_event["run.finished"]["status"] == "failed"


def test_create_run_emits_normalized_event_context(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(json.dumps({"agent": {"workspace": "workspace/writer"}}), encoding="utf-8")

    stdout_lines = "\n".join(
        [
            json.dumps(
                {
                    "type": "event",
                    "event": {
                        "content": {
                            "parts": [
                                {"function_call": {"id": "call_ctx", "name": "inspect_repo", "args": {"path": "."}}},
                                {"function_response": {"id": "call_ctx", "name": "inspect_repo", "response": {"ok": True}}},
                            ]
                        }
                    },
                }
            ),
            json.dumps({"type": "delta", "text": "hello"}),
            json.dumps({"type": "final", "text": "hello world"}),
        ]
    )

    monkeypatch.setattr(
        "openppx.runtime.client_api_service.subprocess.Popen",
        lambda *args, **kwargs: _FakeProcess(stdout_lines),
    )

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    payload = coordinator.create_run("writer", "session_ctx", "hi")
    run_id = payload["data"]["run"]["id"]
    handle = coordinator._runs[run_id]
    assert handle.done.wait(timeout=1.0)

    subscriber = coordinator.stream_run_events(run_id)
    assert subscriber is not None

    envelopes = []
    while True:
        item = subscriber.get(timeout=1.0)
        if item is None:
            break
        envelopes.append((item.event, item.payload))

    by_event = {name: payload for name, payload in envelopes}

    assert by_event["message.created"]["agent_id"] == "writer"
    assert by_event["message.created"]["session_id"] == "session_ctx"
    assert by_event["message.created"]["message_id"] == handle.assistant_message_id
    assert by_event["step.updated"]["step"]["type"] == "step_ref"
    assert by_event["message.delta"]["status"] == "streaming"
    assert by_event["message.completed"]["status"] == "completed"
    assert by_event["run.finished"]["message_id"] == handle.assistant_message_id


def test_cancel_run_emits_cancelled_message_and_run(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(json.dumps({"agent": {"workspace": "workspace/writer"}}), encoding="utf-8")

    monkeypatch.setattr(
        "openppx.runtime.client_api_service.subprocess.Popen",
        lambda *args, **kwargs: _PendingProcess(),
    )

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    payload = coordinator.create_run("writer", "session_cancel", "hi")
    run_id = payload["data"]["run"]["id"]
    cancel_payload = coordinator.cancel_run(run_id)

    assert cancel_payload["ok"] is True

    subscriber = coordinator.stream_run_events(run_id)
    assert subscriber is not None
    events = []
    while True:
        item = subscriber.get(timeout=1.0)
        if item is None:
            break
        events.append((item.event, item.payload))

    by_event = {name: payload for name, payload in events}
    assert by_event["message.cancelled"]["status"] == "cancelled"
    assert by_event["message.cancelled"]["message_id"].startswith("msg_")
    assert by_event["run.cancelled"]["status"] == "cancelled"


def test_create_run_tolerates_null_long_running_tool_ids(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(json.dumps({"agent": {"workspace": "workspace/writer"}}), encoding="utf-8")

    stdout_lines = "\n".join(
        [
            json.dumps(
                {
                    "type": "event",
                    "event": {
                        "long_running_tool_ids": None,
                        "content": {
                            "parts": [
                                {"function_call": {"id": "call_2", "name": "inspect_repo", "args": {"path": "."}}},
                            ]
                        },
                    },
                }
            ),
            json.dumps({"type": "final", "text": "done"}),
        ]
    )

    monkeypatch.setattr(
        "openppx.runtime.client_api_service.subprocess.Popen",
        lambda *args, **kwargs: _FakeProcess(stdout_lines),
    )

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    payload = coordinator.create_run("writer", "session_2", "hi")
    assert payload["ok"] is True

    handle = coordinator._runs[payload["data"]["run"]["id"]]
    assert handle.done.wait(timeout=1.0)
    assert handle.failed is False


def test_client_api_reads_sessions_directly_without_worker(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "database").mkdir()
    config_path = agent_dir / "config.json"
    config_path.write_text(json.dumps({"agent": {"workspace": "workspace/writer"}}), encoding="utf-8")

    async def _seed() -> None:
        service = create_session_service(
            SessionConfig(db_url=f"sqlite+aiosqlite:///{agent_dir / 'database' / 'sessions.db'}")
        )
        async with service:
            session = await service.create_session(
                app_name="openppx",
                user_id="ppx-client-user",
                session_id="writer-seeded",
            )
            await service.append_event(
                session=session,
                event=Event(
                    invocation_id="inv-user",
                    author="user",
                    content=types.Content(
                        role="user",
                        parts=[
                            types.Part.from_text(
                                text=(
                                    "Current request time: 2026-06-10T16:32:17+08:00 (CST)\n"
                                    "Use this as the reference 'now' for relative time expressions in this message.\n\n"
                                    "帮我查一下深圳到青岛的火车和费用"
                                )
                            )
                        ],
                    ),
                ),
            )
            await service.append_event(
                session=session,
                event=Event(
                    invocation_id="inv-1",
                    author="assistant",
                    content=types.Content(role="model", parts=[types.Part.from_text(text="Hello direct path")]),
                ),
            )

    import asyncio

    asyncio.run(_seed())

    monkeypatch.setattr(
        "openppx.runtime.client_api_service._run_worker_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("worker path should not be used")),
    )

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    sessions = coordinator.list_sessions("writer")
    assert sessions["ok"] is True
    assert sessions["data"]["items"][0]["id"] == "writer-seeded"
    assert sessions["data"]["items"][0]["title"] == "帮我查一下深圳到青岛的火车和费用"

    messages = coordinator.get_session_messages("writer-seeded")
    assert messages["ok"] is True
    assert messages["data"]["items"][0]["parts"][0]["text"] == "帮我查一下深圳到青岛的火车和费用"
    assert messages["data"]["items"][1]["parts"][0]["text"] == "Hello direct path"


def test_list_sessions_uses_short_cache(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(json.dumps({"agent": {"workspace": "workspace/writer"}}), encoding="utf-8")

    calls = {"count": 0}

    def _fake_read(self, config_path: Path, *, user_id: str) -> list[dict[str, object]]:
        calls["count"] += 1
        return [{"id": "session-1", "last_update_time": 1_700_000_000, "last_preview": "cached"}]

    monkeypatch.setattr(ClientApiCoordinator, "_read_sessions_direct", _fake_read)

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    first = coordinator.list_sessions("writer")
    second = coordinator.list_sessions("writer")

    assert first["ok"] is True
    assert second["ok"] is True
    assert calls["count"] == 1


def test_create_session_invalidates_session_list_cache(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(json.dumps({"agent": {"workspace": "workspace/writer"}}), encoding="utf-8")

    calls = {"count": 0}

    def _fake_read(self, config_path: Path, *, user_id: str) -> list[dict[str, object]]:
        calls["count"] += 1
        return [{"id": f"session-{calls['count']}", "last_update_time": 1_700_000_000, "last_preview": "cached"}]

    monkeypatch.setattr(ClientApiCoordinator, "_read_sessions_direct", _fake_read)
    monkeypatch.setattr(
        ClientApiCoordinator,
        "_create_session_direct",
        lambda self, config_path, *, user_id, session_id: {"id": session_id, "last_update_time": 1_700_000_001},
    )

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    before = coordinator.list_sessions("writer")
    created = coordinator.create_session("writer")
    after = coordinator.list_sessions("writer")

    assert before["data"]["items"][0]["id"] == "session-1"
    assert created["ok"] is True
    assert after["data"]["items"][0]["id"] == "session-2"
    assert calls["count"] == 2


def test_client_api_owner_can_list_participant_sessions(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "database").mkdir()
    (agent_dir / "config.json").write_text(json.dumps({"agent": {"workspace": "workspace/writer"}}), encoding="utf-8")

    db_path = tmp_path / "identity.db"
    memory_db_path = tmp_path / "memory.db"
    identity_store = IdentityStore(db_path=db_path)
    access_store = AgentAccessStore(db_path=db_path)
    owner = identity_store.put_principal(_principal(principal_id="owner"))
    participant = identity_store.put_principal(_principal(principal_id="participant"))
    access_store.set_agent_owner(agent_id="writer", owner_principal_id=owner.principal_id)
    access_store.upsert_membership(
        AgentMembership(agent_id="writer", principal_id=participant.principal_id, relation="participant")
    )
    policy = AccessPolicy(identity_store=identity_store, agent_access_store=access_store)
    query_service = MemoryQueryService(
        identity_store=identity_store,
        access_policy=policy,
        memory_service=SQLiteMemoryService(db_path=memory_db_path),
        audit_db_path=memory_db_path,
    )

    async def _seed() -> None:
        service = create_session_service(
            SessionConfig(db_url=f"sqlite+aiosqlite:///{agent_dir / 'database' / 'sessions.db'}")
        )
        async with service:
            session = await service.create_session(
                app_name="openppx",
                user_id=participant.principal_id,
                session_id="participant-session",
            )
            await service.append_event(
                session=session,
                event=Event(
                    invocation_id="inv-participant",
                    author="assistant",
                    content=types.Content(role="model", parts=[types.Part.from_text(text="Participant history")]),
                ),
            )

    import asyncio

    asyncio.run(_seed())

    coordinator = ClientApiCoordinator(
        data_dir=tmp_path,
        identity_store=identity_store,
        agent_access_store=access_store,
        access_policy=policy,
        memory_query_service=query_service,
    )
    sessions = coordinator.list_sessions("writer", user_id=owner.principal_id)

    assert sessions["ok"] is True
    assert sessions["data"]["items"][0]["id"] == "participant-session"
    assert sessions["data"]["items"][0]["subject_principal_id"] == participant.principal_id


def test_client_api_owner_cannot_run_in_participant_session(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "database").mkdir()
    (agent_dir / "config.json").write_text(json.dumps({"agent": {"workspace": "workspace/writer"}}), encoding="utf-8")

    db_path = tmp_path / "identity.db"
    memory_db_path = tmp_path / "memory.db"
    identity_store = IdentityStore(db_path=db_path)
    access_store = AgentAccessStore(db_path=db_path)
    owner = identity_store.put_principal(_principal(principal_id="owner"))
    participant = identity_store.put_principal(_principal(principal_id="participant"))
    access_store.set_agent_owner(agent_id="writer", owner_principal_id=owner.principal_id)
    access_store.upsert_membership(
        AgentMembership(agent_id="writer", principal_id=participant.principal_id, relation="participant")
    )
    policy = AccessPolicy(identity_store=identity_store, agent_access_store=access_store)
    query_service = MemoryQueryService(
        identity_store=identity_store,
        access_policy=policy,
        memory_service=SQLiteMemoryService(db_path=memory_db_path),
        audit_db_path=memory_db_path,
    )

    async def _seed() -> None:
        service = create_session_service(
            SessionConfig(db_url=f"sqlite+aiosqlite:///{agent_dir / 'database' / 'sessions.db'}")
        )
        async with service:
            await service.create_session(
                app_name="openppx",
                user_id=participant.principal_id,
                session_id="participant-session",
            )

    import asyncio

    asyncio.run(_seed())

    coordinator = ClientApiCoordinator(
        data_dir=tmp_path,
        identity_store=identity_store,
        agent_access_store=access_store,
        access_policy=policy,
        memory_query_service=query_service,
    )
    payload = coordinator.create_run("writer", "participant-session", "hello", user_id=owner.principal_id)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "ACCESS_DENIED"
    assert payload["error"]["details"]["reason"] == "run_requires_session_owner"


def test_client_api_owner_can_query_participant_memory(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(json.dumps({"agent": {"workspace": "workspace/writer"}}), encoding="utf-8")

    db_path = tmp_path / "identity.db"
    memory_db_path = tmp_path / "memory.db"
    identity_store = IdentityStore(db_path=db_path)
    access_store = AgentAccessStore(db_path=db_path)
    owner = identity_store.put_principal(_principal(principal_id="owner"))
    participant = identity_store.put_principal(_principal(principal_id="participant"))
    access_store.set_agent_owner(agent_id="writer", owner_principal_id=owner.principal_id)
    access_store.upsert_membership(
        AgentMembership(agent_id="writer", principal_id=participant.principal_id, relation="participant")
    )
    policy = AccessPolicy(identity_store=identity_store, agent_access_store=access_store)
    memory_service = SQLiteMemoryService(db_path=memory_db_path)
    asyncio.run(
        memory_service.add_memory(
            app_name="openppx",
            user_id=participant.principal_id,
            memories=[_memory("remember the launch checklist", timestamp="2026-04-18T10:00:00+08:00")],
        )
    )
    query_service = MemoryQueryService(
        identity_store=identity_store,
        access_policy=policy,
        memory_service=memory_service,
        audit_db_path=memory_db_path,
    )

    coordinator = ClientApiCoordinator(
        data_dir=tmp_path,
        identity_store=identity_store,
        agent_access_store=access_store,
        access_policy=policy,
        memory_query_service=query_service,
    )
    payload = coordinator.search_memory("writer", "launch", user_id=owner.principal_id)

    assert payload["ok"] is True
    assert payload["data"]["items"][0]["subject_principal_id"] == participant.principal_id
    assert "launch checklist" in payload["data"]["items"][0]["text"]


def test_client_api_get_agent_access_bootstraps_owner_from_config(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "workspace": "workspace/writer",
                    "privilegeLevel": "high",
                    "ownerPrincipalId": "owner",
                }
            }
        ),
        encoding="utf-8",
    )

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    payload = coordinator.get_agent_access("writer", user_id="owner")

    assert payload["ok"] is True
    assert payload["data"]["agent"]["privilege_level"] == "high"
    assert payload["data"]["agent"]["owner_principal_id"] == "owner"
    assert payload["data"]["agent"]["owner_configured"] is True
    assert payload["data"]["agent"]["metadata"]["owner_source"] == "config"
    assert payload["data"]["requester"]["relation"] == "owner"
    assert payload["data"]["requester"]["scope_kind"] == "agent"
    assert payload["data"]["memberships"] == []


def test_client_api_get_agent_access_filters_memberships_by_visible_scope(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "workspace": "workspace/writer",
                    "ownerPrincipalId": "owner",
                }
            }
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "identity.db"
    identity_store = IdentityStore(db_path=db_path)
    access_store = AgentAccessStore(db_path=db_path)
    participant = identity_store.put_principal(_principal(principal_id="participant"))
    access_store.upsert_membership(
        AgentMembership(agent_id="writer", principal_id=participant.principal_id, relation="participant")
    )
    policy = AccessPolicy(identity_store=identity_store, agent_access_store=access_store)
    query_service = MemoryQueryService(
        identity_store=identity_store,
        access_policy=policy,
        memory_service=SQLiteMemoryService(db_path=tmp_path / "memory.db"),
        audit_db_path=tmp_path / "memory.db",
    )

    coordinator = ClientApiCoordinator(
        data_dir=tmp_path,
        identity_store=identity_store,
        agent_access_store=access_store,
        access_policy=policy,
        memory_query_service=query_service,
    )
    payload = coordinator.get_agent_access("writer", user_id=participant.principal_id)

    assert payload["ok"] is True
    assert payload["data"]["requester"]["relation"] == "participant"
    assert payload["data"]["requester"]["scope_kind"] == "self"
    assert payload["data"]["agent"]["owner_configured"] is True
    assert payload["data"]["agent"]["owner_principal_id"] is None
    assert payload["data"]["memberships"] == [
        {
            "principal_id": "participant",
            "relation": "participant",
            "joined_at_ms": payload["data"]["memberships"][0]["joined_at_ms"],
            "metadata": {},
            "display_name": "participant",
            "principal_type": "human",
            "privilege_level": "minimal",
        }
    ]


def test_client_api_owner_can_manage_participant_membership(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps({"agent": {"workspace": "workspace/writer", "ownerPrincipalId": "owner"}}),
        encoding="utf-8",
    )

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    create_payload = coordinator.upsert_agent_membership("writer", "participant", user_id="owner")
    access_payload = coordinator.get_agent_access("writer", user_id="owner")
    delete_payload = coordinator.delete_agent_membership("writer", "participant", user_id="owner")

    assert create_payload["ok"] is True
    assert create_payload["data"]["membership"]["principal_id"] == "participant"
    assert create_payload["data"]["membership"]["relation"] == "participant"
    assert access_payload["ok"] is True
    assert access_payload["data"]["requester"]["capabilities"]["can_manage_memberships"] is True
    assert access_payload["data"]["requester"]["capabilities"]["can_read_access_audit"] is True
    assert access_payload["data"]["requester"]["capabilities"]["can_read_admin_audit"] is True
    assert access_payload["data"]["requester"]["capabilities"]["can_change_owner"] is False
    assert {item["principal_id"] for item in access_payload["data"]["memberships"]} == {"participant"}
    assert delete_payload["ok"] is True
    assert delete_payload["data"]["deleted"] is True


def test_client_api_participant_cannot_manage_memberships(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps({"agent": {"workspace": "workspace/writer", "ownerPrincipalId": "owner"}}),
        encoding="utf-8",
    )

    db_path = tmp_path / "identity.db"
    identity_store = IdentityStore(db_path=db_path)
    access_store = AgentAccessStore(db_path=db_path)
    participant = identity_store.put_principal(_principal(principal_id="participant"))
    access_store.upsert_membership(
        AgentMembership(agent_id="writer", principal_id=participant.principal_id, relation="participant")
    )
    policy = AccessPolicy(identity_store=identity_store, agent_access_store=access_store)
    query_service = MemoryQueryService(
        identity_store=identity_store,
        access_policy=policy,
        memory_service=SQLiteMemoryService(db_path=tmp_path / "memory.db"),
        audit_db_path=tmp_path / "memory.db",
    )
    coordinator = ClientApiCoordinator(
        data_dir=tmp_path,
        identity_store=identity_store,
        agent_access_store=access_store,
        access_policy=policy,
        memory_query_service=query_service,
    )

    payload = coordinator.upsert_agent_membership("writer", "another-user", user_id=participant.principal_id)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "ACCESS_DENIED"
    assert payload["error"]["details"]["reason"] == "insufficient_agent_admin_role"


def test_client_api_root_can_change_owner(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps({"agent": {"workspace": "workspace/writer", "ownerPrincipalId": "owner"}}),
        encoding="utf-8",
    )

    db_path = tmp_path / "identity.db"
    identity_store = IdentityStore(db_path=db_path)
    access_store = AgentAccessStore(db_path=db_path)
    root = identity_store.put_principal(_principal(principal_id="root-user", privilege_level="root"))
    policy = AccessPolicy(identity_store=identity_store, agent_access_store=access_store)
    query_service = MemoryQueryService(
        identity_store=identity_store,
        access_policy=policy,
        memory_service=SQLiteMemoryService(db_path=tmp_path / "memory.db"),
        audit_db_path=tmp_path / "memory.db",
    )
    coordinator = ClientApiCoordinator(
        data_dir=tmp_path,
        identity_store=identity_store,
        agent_access_store=access_store,
        access_policy=policy,
        memory_query_service=query_service,
    )

    payload = coordinator.set_agent_owner("writer", "new-owner", user_id=root.principal_id)
    access_payload = coordinator.get_agent_access("writer", user_id=root.principal_id)

    assert payload["ok"] is True
    assert payload["data"]["agent"]["owner_principal_id"] == "new-owner"
    assert payload["data"]["agent"]["metadata"]["owner_source"] == "client_api"
    assert access_payload["ok"] is True
    assert access_payload["data"]["agent"]["owner_principal_id"] == "new-owner"
    assert access_payload["data"]["requester"]["capabilities"]["can_read_access_audit"] is True
    assert access_payload["data"]["requester"]["capabilities"]["can_read_admin_audit"] is True
    assert access_payload["data"]["requester"]["capabilities"]["can_change_owner"] is True


def test_client_api_owner_can_read_access_mutation_audit(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps({"agent": {"workspace": "workspace/writer", "ownerPrincipalId": "owner"}}),
        encoding="utf-8",
    )

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    create_payload = coordinator.upsert_agent_membership("writer", "participant", user_id="owner")
    delete_payload = coordinator.delete_agent_membership("writer", "participant", user_id="owner")
    audit_payload = coordinator.get_access_audit("writer", user_id="owner", limit=10, category="mutation")

    assert create_payload["ok"] is True
    assert delete_payload["ok"] is True
    assert audit_payload["ok"] is True
    assert audit_payload["data"]["requester"]["relation"] == "owner"
    assert audit_payload["data"]["category"] == "mutation"
    assert [item["action"] for item in audit_payload["data"]["items"][:2]] == [
        "delete_membership",
        "upsert_membership",
    ]
    newest = audit_payload["data"]["items"][0]
    assert newest["actor_principal_id"] == "owner"
    assert newest["actor_relation"] == "owner"
    assert newest["target_principal_id"] == "participant"
    assert newest["details"]["deleted"] is True


def test_client_api_participant_cannot_read_access_mutation_audit(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps({"agent": {"workspace": "workspace/writer", "ownerPrincipalId": "owner"}}),
        encoding="utf-8",
    )

    db_path = tmp_path / "identity.db"
    identity_store = IdentityStore(db_path=db_path)
    access_store = AgentAccessStore(db_path=db_path)
    participant = identity_store.put_principal(_principal(principal_id="participant"))
    access_store.upsert_membership(
        AgentMembership(agent_id="writer", principal_id=participant.principal_id, relation="participant")
    )
    policy = AccessPolicy(identity_store=identity_store, agent_access_store=access_store)
    query_service = MemoryQueryService(
        identity_store=identity_store,
        access_policy=policy,
        memory_service=SQLiteMemoryService(db_path=tmp_path / "memory.db"),
        audit_db_path=tmp_path / "memory.db",
    )
    coordinator = ClientApiCoordinator(
        data_dir=tmp_path,
        identity_store=identity_store,
        agent_access_store=access_store,
        access_policy=policy,
        memory_query_service=query_service,
    )

    payload = coordinator.get_access_audit("writer", user_id=participant.principal_id, limit=10)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "ACCESS_DENIED"
    assert payload["error"]["details"]["reason"] == "insufficient_agent_admin_role"


def test_client_api_owner_can_read_unified_admin_audit(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps({"agent": {"workspace": "workspace/writer", "ownerPrincipalId": "owner"}}),
        encoding="utf-8",
    )

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    access_payload = coordinator.get_agent_access("writer", user_id="owner")
    create_payload = coordinator.upsert_agent_membership("writer", "participant", user_id="owner")
    admin_audit_payload = coordinator.get_access_audit("writer", user_id="owner", limit=10, category="all")

    assert access_payload["ok"] is True
    assert create_payload["ok"] is True
    assert admin_audit_payload["ok"] is True
    assert admin_audit_payload["data"]["category"] == "all"
    actions = [item["action"] for item in admin_audit_payload["data"]["items"]]
    assert "read_access" in actions
    assert "upsert_membership" in actions
    assert "read_admin_audit" not in actions


def test_client_api_batch_participant_management_supports_dry_run_and_apply(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps({"agent": {"workspace": "workspace/writer", "ownerPrincipalId": "owner"}}),
        encoding="utf-8",
    )

    coordinator = ClientApiCoordinator(data_dir=tmp_path)
    dry_run_payload = coordinator.batch_add_participants(
        "writer",
        ["alice", "bob", "alice"],
        user_id="owner",
        dry_run=True,
    )
    access_before = coordinator.get_agent_access("writer", user_id="owner")
    apply_payload = coordinator.batch_add_participants("writer", ["alice", "bob"], user_id="owner")
    remove_payload = coordinator.batch_remove_participants("writer", ["bob", "nobody"], user_id="owner")
    sync_payload = coordinator.sync_participants("writer", ["carol"], user_id="owner")
    mutation_audit_payload = coordinator.get_access_audit("writer", user_id="owner", limit=10, category="mutation")

    assert dry_run_payload["ok"] is True
    assert dry_run_payload["data"]["dry_run"] is True
    assert dry_run_payload["data"]["applied"] is False
    assert dry_run_payload["data"]["added_principal_ids"] == ["alice", "bob"]
    assert access_before["ok"] is True
    assert access_before["data"]["memberships"] == []

    assert apply_payload["ok"] is True
    assert apply_payload["data"]["added_principal_ids"] == ["alice", "bob"]
    assert remove_payload["ok"] is True
    assert remove_payload["data"]["removed_principal_ids"] == ["bob"]
    assert remove_payload["data"]["unchanged_principal_ids"] == ["nobody"]
    assert sync_payload["ok"] is True
    assert sync_payload["data"]["added_principal_ids"] == ["carol"]
    assert sync_payload["data"]["removed_principal_ids"] == ["alice"]

    access_after = coordinator.get_agent_access("writer", user_id="owner")
    assert {item["principal_id"] for item in access_after["data"]["memberships"]} == {"carol"}

    actions = [item["action"] for item in mutation_audit_payload["data"]["items"]]
    assert actions[:4] == [
        "sync_participants",
        "batch_remove_participants",
        "batch_add_participants",
        "batch_add_participants",
    ]
    assert mutation_audit_payload["data"]["items"][0]["details"]["dry_run"] is False
    assert mutation_audit_payload["data"]["items"][3]["details"]["dry_run"] is True


def test_client_api_participant_batch_management_is_denied_and_audited(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps({"agent": {"workspace": "workspace/writer", "ownerPrincipalId": "owner"}}),
        encoding="utf-8",
    )

    db_path = tmp_path / "identity.db"
    identity_store = IdentityStore(db_path=db_path)
    access_store = AgentAccessStore(db_path=db_path)
    owner = identity_store.put_principal(_principal(principal_id="owner"))
    participant = identity_store.put_principal(_principal(principal_id="participant"))
    access_store.set_agent_owner(agent_id="writer", owner_principal_id=owner.principal_id)
    access_store.upsert_membership(
        AgentMembership(agent_id="writer", principal_id=participant.principal_id, relation="participant")
    )
    policy = AccessPolicy(identity_store=identity_store, agent_access_store=access_store)
    query_service = MemoryQueryService(
        identity_store=identity_store,
        access_policy=policy,
        memory_service=SQLiteMemoryService(db_path=tmp_path / "memory.db"),
        audit_db_path=tmp_path / "memory.db",
    )
    coordinator = ClientApiCoordinator(
        data_dir=tmp_path,
        identity_store=identity_store,
        agent_access_store=access_store,
        access_policy=policy,
        memory_query_service=query_service,
    )

    denied_payload = coordinator.batch_add_participants(
        "writer",
        ["another-user"],
        user_id=participant.principal_id,
    )
    admin_audit_payload = coordinator.get_access_audit("writer", user_id=owner.principal_id, limit=10, category="all")

    assert denied_payload["ok"] is False
    assert denied_payload["error"]["details"]["reason"] == "insufficient_agent_admin_role"
    newest = admin_audit_payload["data"]["items"][0]
    assert newest["action"] == "batch_add_participants"
    assert newest["details"]["allowed"] is False


def test_client_api_owner_can_read_memory_audit(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps({"agent": {"workspace": "workspace/writer", "ownerPrincipalId": "owner"}}),
        encoding="utf-8",
    )

    db_path = tmp_path / "identity.db"
    memory_db_path = tmp_path / "memory.db"
    identity_store = IdentityStore(db_path=db_path)
    access_store = AgentAccessStore(db_path=db_path)
    owner = identity_store.put_principal(_principal(principal_id="owner"))
    participant = identity_store.put_principal(_principal(principal_id="participant"))
    access_store.set_agent_owner(agent_id="writer", owner_principal_id=owner.principal_id)
    access_store.upsert_membership(
        AgentMembership(agent_id="writer", principal_id=participant.principal_id, relation="participant")
    )
    policy = AccessPolicy(identity_store=identity_store, agent_access_store=access_store)
    query_service = MemoryQueryService(
        identity_store=identity_store,
        access_policy=policy,
        memory_service=SQLiteMemoryService(db_path=memory_db_path),
        audit_db_path=memory_db_path,
    )
    asyncio.run(
        query_service.search(
            agent_id="writer",
            requester_principal_id=participant.principal_id,
            query="launch",
        )
    )

    coordinator = ClientApiCoordinator(
        data_dir=tmp_path,
        identity_store=identity_store,
        agent_access_store=access_store,
        access_policy=policy,
        memory_query_service=query_service,
    )
    payload = coordinator.get_memory_audit("writer", user_id=owner.principal_id, limit=10)

    assert payload["ok"] is True
    assert payload["data"]["requester"]["relation"] == "owner"
    assert payload["data"]["items"][0]["requester_principal_id"] == participant.principal_id


def test_client_api_participant_memory_audit_stays_self_scoped(tmp_path: Path) -> None:
    (tmp_path / "global_config.json").write_text(
        json.dumps({"agents": [{"name": "writer", "enabled": True}]}),
        encoding="utf-8",
    )
    agent_dir = tmp_path / "writer"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        json.dumps({"agent": {"workspace": "workspace/writer", "ownerPrincipalId": "owner"}}),
        encoding="utf-8",
    )

    db_path = tmp_path / "identity.db"
    memory_db_path = tmp_path / "memory.db"
    identity_store = IdentityStore(db_path=db_path)
    access_store = AgentAccessStore(db_path=db_path)
    participant = identity_store.put_principal(_principal(principal_id="participant"))
    other = identity_store.put_principal(_principal(principal_id="other"))
    access_store.upsert_membership(
        AgentMembership(agent_id="writer", principal_id=participant.principal_id, relation="participant")
    )
    access_store.upsert_membership(
        AgentMembership(agent_id="writer", principal_id=other.principal_id, relation="participant")
    )
    policy = AccessPolicy(identity_store=identity_store, agent_access_store=access_store)
    query_service = MemoryQueryService(
        identity_store=identity_store,
        access_policy=policy,
        memory_service=SQLiteMemoryService(db_path=memory_db_path),
        audit_db_path=memory_db_path,
    )
    asyncio.run(
        query_service.search(
            agent_id="writer",
            requester_principal_id=participant.principal_id,
            query="alpha",
        )
    )
    asyncio.run(
        query_service.search(
            agent_id="writer",
            requester_principal_id=other.principal_id,
            query="beta",
        )
    )

    coordinator = ClientApiCoordinator(
        data_dir=tmp_path,
        identity_store=identity_store,
        agent_access_store=access_store,
        access_policy=policy,
        memory_query_service=query_service,
    )
    payload = coordinator.get_memory_audit("writer", user_id=participant.principal_id, limit=10)

    assert payload["ok"] is True
    assert payload["data"]["requester"]["scope_kind"] == "self"
    assert [item["requester_principal_id"] for item in payload["data"]["items"]] == [participant.principal_id]
