"""Tests for bus and gateway skeleton."""

from __future__ import annotations

import asyncio
import os
import tempfile
import types as pytypes
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from google.adk.agents import LlmAgent
from google.adk.tools import LongRunningFunctionTool

from openheron.bus.events import InboundMessage, OutboundMessage
from openheron.bus.queue import MessageBus
from openheron.app.gateway import Gateway
from openheron.runtime.cron_service import CronJob, CronJobState, CronPayload, CronSchedule
from openheron.runtime.heartbeat_runner import HeartbeatRunRequest
from openheron.runtime.heartbeat_status_store import read_heartbeat_status_snapshot
from openheron.runtime.agent_runtime import AgentRuntimeContext
from openheron.tooling.registry import SubagentSpawnRequest


class MessageBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip(self) -> None:
        bus = MessageBus()
        inbound = InboundMessage(
            channel="local",
            sender_id="u1",
            chat_id="c1",
            content="ping",
            media=["/tmp/demo.png"],
        )
        outbound = OutboundMessage(channel="local", chat_id="c1", content="pong")

        await bus.publish_inbound(inbound)
        await bus.publish_outbound(outbound)

        got_inbound = await bus.consume_inbound()
        got_outbound = await bus.consume_outbound()

        self.assertEqual(got_inbound.content, "ping")
        self.assertEqual(got_inbound.media, ["/tmp/demo.png"])
        self.assertEqual(got_outbound.content, "pong")


class GatewayTests(unittest.TestCase):
    def test_process_message_collects_final_text(self) -> None:
        fake_event_1 = pytypes.SimpleNamespace(content=pytypes.SimpleNamespace(parts=[]))
        fake_event_2 = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="gateway answer")])
        )
        captured: dict[str, object] = {}

        class _FakeRunner:
            async def run_async(self, **kwargs):
                captured.update(kwargs)
                yield fake_event_1
                yield fake_event_2

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())
            inbound = InboundMessage(
                channel="local",
                sender_id="u1",
                chat_id="c1",
                content="hello",
                timestamp=datetime(2026, 2, 18, 9, 30, tzinfo=timezone.utc),
            )
            outbound = asyncio.run(gateway.process_message(inbound))

        self.assertEqual(outbound.channel, "local")
        self.assertEqual(outbound.chat_id, "c1")
        self.assertEqual(outbound.content, "gateway answer")
        request = captured["new_message"]
        text = request.parts[0].text
        self.assertIn("Current request time: 2026-02-18T09:30:00+00:00 (UTC)", text)
        self.assertIn("Use this as the reference 'now' for relative time expressions", text)
        self.assertIn("\n\nhello", text)
        route_meta = outbound.metadata.get("openheron_route", {})
        self.assertEqual(route_meta.get("agentId"), "main")
        self.assertEqual(route_meta.get("matchedBy"), "default")
        self.assertEqual(route_meta.get("guildId"), "")
        self.assertEqual(route_meta.get("teamId"), "")
        self.assertEqual(route_meta.get("roles"), [])
        self.assertEqual(route_meta.get("sessionId"), "agent:main:local:default:direct:c1")

    def test_process_message_merges_stream_snapshots(self) -> None:
        fake_event_1 = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="hello")])
        )
        fake_event_2 = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="hello world")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event_1
                yield fake_event_2

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())
            inbound = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="hello")
            outbound = asyncio.run(gateway.process_message(inbound))

        self.assertEqual(outbound.content, "hello world")

    def test_process_message_help_command_skips_runner(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="unused")])
        )
        captured_calls: list[dict[str, object]] = []

        class _FakeRunner:
            async def run_async(self, **kwargs):
                captured_calls.append(kwargs)
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())
            inbound = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="/help")
            outbound = asyncio.run(gateway.process_message(inbound))

        self.assertIn("/new", outbound.content)
        self.assertIn("/help", outbound.content)
        self.assertEqual(captured_calls, [])
        self.assertEqual(outbound.metadata.get("openheron_route", {}).get("agentId"), "main")

    def test_process_message_new_command_rotates_session_id(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="ok")])
        )
        captured_calls: list[dict[str, object]] = []

        class _FakeRunner:
            async def run_async(self, **kwargs):
                captured_calls.append(kwargs)
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())

            first = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="hello")
            first_outbound = asyncio.run(gateway.process_message(first))
            self.assertEqual(first_outbound.content, "ok")

            reset = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="/new")
            reset_outbound = asyncio.run(gateway.process_message(reset))
            self.assertEqual(reset_outbound.content, "Started a new conversation session.")

            second = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="hello again")
            second_outbound = asyncio.run(gateway.process_message(second))
        self.assertEqual(second_outbound.content, "ok")

        self.assertEqual(len(captured_calls), 2)
        self.assertEqual(captured_calls[0]["session_id"], "agent:main:local:default:direct:c1")
        rotated_session_id = captured_calls[1]["session_id"]
        self.assertNotEqual(rotated_session_id, "agent:main:local:default:direct:c1")
        self.assertTrue(str(rotated_session_id).startswith("agent:main:local:default:direct:c1:new:"))

    def test_process_message_new_command_persists_current_session_to_memory(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="ok")])
        )
        fake_session = pytypes.SimpleNamespace(id="local:c1")
        memory_service = pytypes.SimpleNamespace(add_session_to_memory=AsyncMock(return_value=None))
        session_service = pytypes.SimpleNamespace(get_session=AsyncMock(return_value=fake_session))

        class _FakeRunner:
            def __init__(self, *, memory_service, app_name: str) -> None:
                self.memory_service = memory_service
                self.app_name = app_name

            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch(
            "openheron.app.gateway.create_runner",
            side_effect=[
                (_FakeRunner(memory_service=memory_service, app_name="openheron"), session_service),
                (_FakeRunner(memory_service=memory_service, app_name="openheron"), session_service),
            ],
        ):
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())
            inbound = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="/new")
            outbound = asyncio.run(gateway.process_message(inbound))

        self.assertEqual(outbound.content, "Started a new conversation session.")
        session_service.get_session.assert_awaited_once_with(
            app_name="openheron",
            user_id="agent:main:u1",
            session_id="agent:main:local:default:direct:c1",
        )
        memory_service.add_session_to_memory.assert_awaited_once_with(fake_session)

    def test_process_message_routes_by_account_and_applies_agent_runtime_context(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="ok")])
        )
        captured: list[dict[str, object]] = []

        class _FakeRunner:
            async def run_async(self, **kwargs):
                from openheron.runtime.agent_runtime import get_current_agent_runtime

                runtime = get_current_agent_runtime()
                captured.append(
                    {
                        "session_id": kwargs.get("session_id"),
                        "user_id": kwargs.get("user_id"),
                        "agent_id": runtime.agent_id if runtime else None,
                        "allow_exec": runtime.allow_exec if runtime else None,
                    }
                )
                yield fake_event

        config = {
            "agents": {
                "defaults": {
                    "workspace": "/tmp/main",
                    "agentDir": "/tmp/main/agent",
                    "security": {"allowExec": True},
                },
                "list": [
                    {
                        "id": "main",
                        "default": True,
                        "workspace": "/tmp/main",
                        "agentDir": "/tmp/main/agent",
                    },
                    {
                        "id": "personal",
                        "workspace": "/tmp/personal",
                        "agentDir": "/tmp/personal/agent",
                        "security": {"allowExec": True},
                    },
                    {
                        "id": "business",
                        "workspace": "/tmp/business",
                        "agentDir": "/tmp/business/agent",
                        "security": {"allowExec": False},
                    },
                ],
            },
            "bindings": [
                {"agentId": "personal", "match": {"channel": "whatsapp", "accountId": "personal"}},
                {"agentId": "business", "match": {"channel": "whatsapp", "accountId": "business"}},
            ],
        }

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus(), config=config)
            personal_msg = InboundMessage(
                channel="whatsapp",
                sender_id="u1",
                chat_id="+10001",
                content="hello",
                metadata={"accountId": "personal", "peer": {"kind": "direct", "id": "+10001"}},
            )
            business_msg = InboundMessage(
                channel="whatsapp",
                sender_id="u1",
                chat_id="+10001",
                content="hello",
                metadata={"accountId": "business", "peer": {"kind": "direct", "id": "+10001"}},
            )

            self.assertEqual(asyncio.run(gateway.process_message(personal_msg)).content, "ok")
            self.assertEqual(asyncio.run(gateway.process_message(business_msg)).content, "ok")

        self.assertEqual(captured[0]["agent_id"], "personal")
        self.assertEqual(captured[0]["user_id"], "agent:personal:u1")
        self.assertEqual(captured[0]["session_id"], "agent:personal:whatsapp:personal:direct:+10001")
        self.assertEqual(captured[0]["allow_exec"], True)

        self.assertEqual(captured[1]["agent_id"], "business")
        self.assertEqual(captured[1]["user_id"], "agent:business:u1")
        self.assertEqual(captured[1]["session_id"], "agent:business:whatsapp:business:direct:+10001")
        self.assertEqual(captured[1]["allow_exec"], False)

    def test_process_message_persists_route_stats_snapshot(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="ok")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            with patch("openheron.app.gateway.write_route_stats_snapshot") as mocked_write:
                gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())
                inbound = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="hello")
                outbound = asyncio.run(gateway.process_message(inbound))

        self.assertEqual(outbound.content, "ok")
        self.assertTrue(mocked_write.called)
        payload = mocked_write.call_args.args[1]
        self.assertEqual(payload.get("totalMessages"), 1)
        self.assertEqual(payload.get("byAgent", {}).get("main"), 1)
        self.assertEqual(payload.get("byChannel", {}).get("local"), 1)
        recent = payload.get("recent", [])
        self.assertTrue(isinstance(recent, list) and len(recent) == 1)
        self.assertEqual(recent[0].get("guildId"), "")
        self.assertEqual(recent[0].get("teamId"), "")
        self.assertEqual(recent[0].get("roles"), [])


class GatewayLoopResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_consume_inbound_continues_after_processing_error(self) -> None:
        class _FakeRunner:
            async def run_async(self, **kwargs):
                if False:
                    yield  # pragma: no cover

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=bus)

        success_outbound = OutboundMessage(channel="local", chat_id="c2", content="ok")
        gateway.process_message = AsyncMock(side_effect=[RuntimeError("boom"), success_outbound])  # type: ignore[method-assign]

        task = asyncio.create_task(gateway._consume_inbound())
        try:
            await bus.publish_inbound(InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="one"))
            await bus.publish_inbound(InboundMessage(channel="local", sender_id="u1", chat_id="c2", content="two"))

            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
            self.assertEqual(outbound.chat_id, "c2")
            self.assertEqual(outbound.content, "ok")
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class GatewayCronTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _runtime(agent_dir: Path | None = None) -> AgentRuntimeContext:
        root = agent_dir or Path("/tmp/openheron-test-main")
        return AgentRuntimeContext(
            agent_id="main",
            workspace_root=root / "workspace",
            agent_dir=root,
        )

    async def test_start_and_stop_manage_cron_and_heartbeat_service(self) -> None:
        class _FakeRunner:
            async def run_async(self, **kwargs):
                if False:
                    yield  # pragma: no cover

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        fake_cron_service = pytypes.SimpleNamespace(start=AsyncMock(), stop=Mock())
        fake_heartbeat_runner = pytypes.SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        heartbeat_waker = Mock()
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            with (
                patch("openheron.app.gateway.CronService", return_value=fake_cron_service),
                patch("openheron.app.gateway.HeartbeatRunner", return_value=fake_heartbeat_runner),
                patch("openheron.app.gateway.configure_heartbeat_waker", heartbeat_waker),
            ):
                gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())
                await gateway.start()
                fake_cron_service.start.assert_awaited_once()
                fake_heartbeat_runner.start.assert_awaited_once()
                heartbeat_waker.assert_any_call(gateway._request_heartbeat_wake)
                await gateway.stop()
                fake_cron_service.stop.assert_called_once()
                fake_heartbeat_runner.stop.assert_awaited_once()
                heartbeat_waker.assert_called_with(None)

    async def test_run_heartbeat_executes_runner_prompt(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="heartbeat response")])
        )
        captured: dict[str, object] = {}

        class _FakeRunner:
            async def run_async(self, **kwargs):
                captured.update(kwargs)
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())

        req = HeartbeatRunRequest(reason="manual", prompt="ops check")
        await gateway._run_heartbeat(req, agent_runtime=self._runtime())

        self.assertEqual(captured["user_id"], "heartbeat")
        self.assertEqual(captured["session_id"], "heartbeat:main")
        request = captured["new_message"]
        self.assertIn("ops check", request.parts[0].text)
        self.assertIn("Current time:", request.parts[0].text)

    async def test_run_heartbeat_skips_ok_delivery_when_show_ok_disabled(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="HEARTBEAT_OK")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=bus)

        req = HeartbeatRunRequest(reason="interval", prompt="ops check")
        with patch.dict(os.environ, {"OPENHERON_HEARTBEAT_SHOW_OK": "0"}, clear=False):
            await gateway._run_heartbeat(req, agent_runtime=self._runtime())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), timeout=0.05)

    async def test_run_heartbeat_delivers_ok_when_show_ok_enabled(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="HEARTBEAT_OK")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=bus)

        req = HeartbeatRunRequest(reason="interval", prompt="ops check")
        with patch.dict(os.environ, {"OPENHERON_HEARTBEAT_SHOW_OK": "1"}, clear=False):
            await gateway._run_heartbeat(req, agent_runtime=self._runtime())
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=0.2)
        self.assertEqual(outbound.channel, "local")
        self.assertEqual(outbound.chat_id, "heartbeat")
        self.assertEqual(outbound.content, "HEARTBEAT_OK")

    async def test_run_heartbeat_honors_show_alerts_for_non_ack_payloads(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="HEARTBEAT_OK alert disk full")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=bus)

        req = HeartbeatRunRequest(reason="interval", prompt="ops check")
        with patch.dict(
            os.environ,
            {
                "OPENHERON_HEARTBEAT_SHOW_OK": "0",
                "OPENHERON_HEARTBEAT_SHOW_ALERTS": "0",
                "OPENHERON_HEARTBEAT_ACK_MAX_CHARS": "0",
            },
            clear=False,
        ):
            await gateway._run_heartbeat(req, agent_runtime=self._runtime())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), timeout=0.05)

        with patch.dict(
            os.environ,
            {
                "OPENHERON_HEARTBEAT_SHOW_OK": "0",
                "OPENHERON_HEARTBEAT_SHOW_ALERTS": "1",
                "OPENHERON_HEARTBEAT_ACK_MAX_CHARS": "0",
            },
            clear=False,
        ):
            await gateway._run_heartbeat(req, agent_runtime=self._runtime())
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=0.2)
        self.assertEqual(outbound.content, "alert disk full")

    async def test_run_heartbeat_routes_to_last_inbound_target(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="alert disk full")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=bus)
        gateway._last_inbound_route = ("feishu", "chat-ops")

        req = HeartbeatRunRequest(reason="manual", prompt="ops check")
        with patch.dict(os.environ, {"OPENHERON_HEARTBEAT_TARGET": "last"}, clear=False):
            await gateway._run_heartbeat(req, agent_runtime=self._runtime())

        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=0.2)
        self.assertEqual(outbound.channel, "feishu")
        self.assertEqual(outbound.chat_id, "chat-ops")
        self.assertEqual(outbound.content, "alert disk full")

    async def test_run_heartbeat_target_none_disables_delivery(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="alert disk full")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=bus)

        req = HeartbeatRunRequest(reason="manual", prompt="ops check")
        with patch.dict(os.environ, {"OPENHERON_HEARTBEAT_TARGET": "none"}, clear=False):
            await gateway._run_heartbeat(req, agent_runtime=self._runtime())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), timeout=0.05)

    async def test_run_heartbeat_target_channel_uses_explicit_channel_and_chat(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="HEARTBEAT_OK")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=bus)

        req = HeartbeatRunRequest(reason="interval", prompt="ops check")
        with patch.dict(
            os.environ,
            {
                "OPENHERON_HEARTBEAT_SHOW_OK": "1",
                "OPENHERON_HEARTBEAT_TARGET": "channel",
                "OPENHERON_HEARTBEAT_TARGET_CHANNEL": "slack",
                "OPENHERON_HEARTBEAT_TARGET_CHAT_ID": "C123",
            },
            clear=False,
        ):
            await gateway._run_heartbeat(req, agent_runtime=self._runtime())
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=0.2)
        self.assertEqual(outbound.channel, "slack")
        self.assertEqual(outbound.chat_id, "C123")
        self.assertEqual(outbound.content, "HEARTBEAT_OK")

    async def test_heartbeat_status_exposes_last_delivery(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="HEARTBEAT_OK alert disk full now")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())

        req = HeartbeatRunRequest(reason="manual", prompt="ops check")
        with patch.dict(
            os.environ,
            {
                "OPENHERON_HEARTBEAT_TARGET": "channel",
                "OPENHERON_HEARTBEAT_TARGET_CHANNEL": "feishu",
                "OPENHERON_HEARTBEAT_TARGET_CHAT_ID": "ops-room",
                "OPENHERON_HEARTBEAT_SHOW_ALERTS": "1",
                "OPENHERON_HEARTBEAT_ACK_MAX_CHARS": "0",
            },
            clear=False,
        ):
            await gateway._run_heartbeat(req, agent_runtime=self._runtime())
            status = gateway.heartbeat_status()

        self.assertEqual(status["target_mode"], "channel")
        self.assertEqual(status["last_delivery"]["kind"], "alert")
        self.assertEqual(status["last_delivery"]["target_channel"], "feishu")
        self.assertEqual(status["last_delivery"]["target_chat_id"], "ops-room")
        self.assertIn("alert disk full", status["last_delivery"]["content_preview"])

    async def test_run_heartbeat_persists_status_snapshot(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="HEARTBEAT_OK")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())

        req = HeartbeatRunRequest(reason="manual", prompt="ops check")
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(Path(tmp))
            with patch.dict(os.environ, {"OPENHERON_HEARTBEAT_SHOW_OK": "1"}, clear=False):
                await gateway._run_heartbeat(req, agent_runtime=runtime)
            snapshot = read_heartbeat_status_snapshot(Path(tmp))

        self.assertIsNotNone(snapshot)
        self.assertTrue(bool(snapshot and snapshot.get("last_delivery", {}).get("delivered")))
        self.assertEqual(snapshot and snapshot.get("last_delivery", {}).get("kind"), "ok")

    async def test_heartbeat_status_before_start_has_safe_defaults(self) -> None:
        class _FakeRunner:
            async def run_async(self, **kwargs):
                if False:
                    yield  # pragma: no cover

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())

        status = gateway.heartbeat_status()
        self.assertFalse(bool(status["running"]))
        self.assertFalse(bool(status["enabled"]))
        self.assertEqual(status["last_delivery"], {})

    async def test_run_cron_job_delivers_outbound_when_enabled(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="cron answer")])
        )
        captured: dict[str, object] = {}

        class _FakeRunner:
            async def run_async(self, **kwargs):
                captured.update(kwargs)
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=bus)

        job = CronJob(
            id="job12345",
            name="demo",
            enabled=True,
            schedule=CronSchedule(kind="every", every_seconds=60),
            payload=CronPayload(message="do work", deliver=True, channel="local", to="c1"),
            state=CronJobState(),
            created_at_ms=0,
            updated_at_ms=0,
        )
        result = await gateway._run_cron_job(job)
        self.assertEqual(result, "cron answer")
        request = captured["new_message"]
        text = request.parts[0].text
        self.assertTrue(text.startswith("do work"))
        self.assertIn("Current time:", text)

        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        self.assertEqual(outbound.channel, "local")
        self.assertEqual(outbound.chat_id, "c1")
        self.assertEqual(outbound.content, "cron answer")

    async def test_run_cron_job_triggers_heartbeat_wake(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="cron answer")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())

        fake_heartbeat_runner = pytypes.SimpleNamespace(request_wake=Mock())
        gateway._heartbeat_runner = fake_heartbeat_runner

        job = CronJob(
            id="job12345",
            name="demo",
            enabled=True,
            schedule=CronSchedule(kind="every", every_seconds=60),
            payload=CronPayload(message="do work", deliver=False, channel="local", to="c1"),
            state=CronJobState(),
            created_at_ms=0,
            updated_at_ms=0,
        )
        await gateway._run_cron_job(job)
        fake_heartbeat_runner.request_wake.assert_called_once_with(reason="cron:job12345", coalesce_ms=0)

    async def test_run_cron_job_without_heartbeat_runner_still_succeeds(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="cron answer")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=MessageBus())

        job = CronJob(
            id="job12345",
            name="demo",
            enabled=True,
            schedule=CronSchedule(kind="every", every_seconds=60),
            payload=CronPayload(message="do work", deliver=False, channel="local", to="c1"),
            state=CronJobState(),
            created_at_ms=0,
            updated_at_ms=0,
        )
        result = await gateway._run_cron_job(job)
        self.assertEqual(result, "cron answer")


class GatewaySubagentTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_subagent_resumes_parent_and_notifies(self) -> None:
        subagent_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="background done")])
        )
        resume_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="parent resumed notify")])
        )
        captured_calls: list[dict[str, object]] = []

        class _FakeRunner:
            async def run_async(self, **kwargs):
                captured_calls.append(kwargs)
                if kwargs.get("invocation_id"):
                    yield resume_event
                else:
                    yield subagent_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=bus)

        request = SubagentSpawnRequest(
            task_id="subagent-abc",
            prompt="do work",
            user_id="u1",
            session_id="parent-session",
            invocation_id="inv-1",
            function_call_id="fc-1",
            channel="local",
            chat_id="c1",
            notify_on_complete=True,
        )
        task = gateway._dispatch_subagent_request(request)
        self.assertIsNotNone(task)
        await asyncio.wait_for(task, timeout=0.5)

        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        self.assertEqual(outbound.channel, "local")
        self.assertEqual(outbound.chat_id, "c1")
        self.assertEqual(outbound.content, "parent resumed notify")
        self.assertEqual(len(captured_calls), 2)
        self.assertEqual(captured_calls[0]["session_id"], "subagent:subagent-abc")
        self.assertEqual(captured_calls[1]["session_id"], "parent-session")
        self.assertEqual(captured_calls[1]["invocation_id"], "inv-1")

    async def test_background_subagent_can_skip_notification(self) -> None:
        subagent_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="background done")])
        )
        resume_event = pytypes.SimpleNamespace(content=pytypes.SimpleNamespace(parts=[]))

        class _FakeRunner:
            async def run_async(self, **kwargs):
                if kwargs.get("invocation_id"):
                    yield resume_event
                else:
                    yield subagent_event

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        with patch("openheron.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openheron", bus=bus)

        request = SubagentSpawnRequest(
            task_id="subagent-def",
            prompt="do work",
            user_id="u1",
            session_id="parent-session",
            invocation_id="inv-1",
            function_call_id="fc-2",
            channel="local",
            chat_id="c1",
            notify_on_complete=False,
        )
        task = gateway._dispatch_subagent_request(request)
        self.assertIsNotNone(task)
        await asyncio.wait_for(task, timeout=0.5)

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), timeout=0.05)

    def test_subagent_runner_uses_restricted_toolset_without_spawn(self) -> None:
        def read_stub(path: str) -> str:
            return path

        def spawn_subagent(prompt: str) -> dict[str, str]:
            return {"status": "pending", "task_id": "x"}

        root = LlmAgent(
            name="openheron",
            model="gemini-2.0-flash",
            instruction="test",
            tools=[
                read_stub,
                LongRunningFunctionTool(func=spawn_subagent),
            ],
        )

        created_agents: list[object] = []

        class _FakeRunner:
            async def run_async(self, **kwargs):
                if False:
                    yield  # pragma: no cover

        def _create_runner_side_effect(*, agent, app_name, session_service=None):
            created_agents.append(agent)
            return _FakeRunner(), object()

        with patch("openheron.app.gateway.create_runner", side_effect=_create_runner_side_effect):
            Gateway(agent=root, app_name=root.name, bus=MessageBus())

        self.assertEqual(len(created_agents), 2)
        parent_agent = created_agents[0]
        subagent = created_agents[1]

        parent_tool_names = [getattr(tool, "name", getattr(tool, "__name__", str(tool))) for tool in parent_agent.tools]
        subagent_tool_names = [getattr(tool, "name", getattr(tool, "__name__", str(tool))) for tool in subagent.tools]
        self.assertIn("spawn_subagent", parent_tool_names)
        self.assertNotIn("spawn_subagent", subagent_tool_names)


if __name__ == "__main__":
    unittest.main()
