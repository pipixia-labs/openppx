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
from google.adk.agents.run_config import StreamingMode
from google.adk.tools import LongRunningFunctionTool

from openpipixia.bus.events import InboundMessage, OutboundMessage
from openpipixia.bus.queue import MessageBus
from openpipixia.app.gateway import Gateway
from openpipixia.runtime.cron_service import CronJob, CronJobState, CronPayload, CronSchedule
from openpipixia.runtime.heartbeat_runner import HeartbeatRunRequest
from openpipixia.runtime.heartbeat_status_store import read_heartbeat_status_snapshot
from openpipixia.runtime.heartbeat_utils import DEFAULT_HEARTBEAT_PROMPT
from openpipixia.tooling.registry import SubagentSpawnRequest


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

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())
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

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())
            inbound = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="hello")
            outbound = asyncio.run(gateway.process_message(inbound))

        self.assertEqual(outbound.content, "hello world")

    def test_process_message_streams_deltas_when_requested(self) -> None:
        fake_event_1 = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="hello")])
        )
        fake_event_2 = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="hello world")])
        )
        captured: dict[str, object] = {}

        class _FakeRunner:
            async def run_async(self, **kwargs):
                captured.update(kwargs)
                yield fake_event_1
                yield fake_event_2

        async def _run() -> tuple[OutboundMessage, list[OutboundMessage]]:
            bus = MessageBus()
            fake_agent = pytypes.SimpleNamespace(name="openpipixia")
            with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
                gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=bus)
                inbound = InboundMessage(
                    channel="local",
                    sender_id="u1",
                    chat_id="c1",
                    content="hello",
                    metadata={"_wants_stream": True},
                )
                outbound = await gateway.process_message(inbound)
                streamed = [
                    await asyncio.wait_for(bus.consume_outbound(), timeout=0.2),
                    await asyncio.wait_for(bus.consume_outbound(), timeout=0.2),
                    await asyncio.wait_for(bus.consume_outbound(), timeout=0.2),
                ]
                return outbound, streamed

        outbound, streamed = asyncio.run(_run())

        self.assertEqual(outbound.content, "hello world")
        self.assertTrue(outbound.metadata.get("_streamed"))
        self.assertEqual(streamed[0].metadata.get("_stream_delta"), True)
        self.assertEqual(streamed[0].content, "hello")
        self.assertEqual(streamed[1].metadata.get("_stream_delta"), True)
        self.assertEqual(streamed[1].content, " world")
        self.assertEqual(streamed[2].metadata.get("_stream_end"), True)
        run_config = captured.get("run_config")
        self.assertIsNotNone(run_config)
        self.assertEqual(run_config.streaming_mode, StreamingMode.SSE)

    def test_process_message_skips_final_aggregate_after_delta_chunks(self) -> None:
        fake_event_1 = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="hello")])
        )
        fake_event_2 = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text=" world")])
        )
        fake_event_3 = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="hello world")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event_1
                yield fake_event_2
                yield fake_event_3

        async def _run() -> tuple[OutboundMessage, list[OutboundMessage]]:
            bus = MessageBus()
            fake_agent = pytypes.SimpleNamespace(name="openpipixia")
            with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
                gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=bus)
                inbound = InboundMessage(
                    channel="local",
                    sender_id="u1",
                    chat_id="c1",
                    content="hello",
                    metadata={"_wants_stream": True},
                )
                outbound = await gateway.process_message(inbound)
                streamed = [
                    await asyncio.wait_for(bus.consume_outbound(), timeout=0.2),
                    await asyncio.wait_for(bus.consume_outbound(), timeout=0.2),
                    await asyncio.wait_for(bus.consume_outbound(), timeout=0.2),
                ]
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(bus.consume_outbound(), timeout=0.05)
                return outbound, streamed

        outbound, streamed = asyncio.run(_run())

        self.assertEqual(outbound.content, "hello world")
        self.assertEqual(streamed[0].content, "hello")
        self.assertEqual(streamed[1].content, " world")
        self.assertEqual(streamed[2].metadata.get("_stream_end"), True)

    def test_process_message_help_command_skips_runner(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="unused")])
        )
        captured_calls: list[dict[str, object]] = []

        class _FakeRunner:
            async def run_async(self, **kwargs):
                captured_calls.append(kwargs)
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())
            inbound = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="/help")
            outbound = asyncio.run(gateway.process_message(inbound))

        self.assertIn("/new", outbound.content)
        self.assertIn("/help", outbound.content)
        self.assertEqual(captured_calls, [])

    def test_process_message_new_command_rotates_session_id(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="ok")])
        )
        captured_calls: list[dict[str, object]] = []

        class _FakeRunner:
            async def run_async(self, **kwargs):
                captured_calls.append(kwargs)
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())

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
        self.assertEqual(captured_calls[0]["session_id"], "local:c1")
        rotated_session_id = captured_calls[1]["session_id"]
        self.assertNotEqual(rotated_session_id, "local:c1")
        self.assertTrue(str(rotated_session_id).startswith("local:c1:new:"))

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

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch(
            "openpipixia.app.gateway.create_runner",
            side_effect=[
                (_FakeRunner(memory_service=memory_service, app_name="openpipixia"), session_service),
                (_FakeRunner(memory_service=memory_service, app_name="openpipixia"), session_service),
            ],
        ):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())
            inbound = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="/new")
            outbound = asyncio.run(gateway.process_message(inbound))

        self.assertEqual(outbound.content, "Started a new conversation session.")
        session_service.get_session.assert_awaited_once_with(
            app_name="openpipixia",
            user_id="u1",
            session_id="local:c1",
        )
        memory_service.add_session_to_memory.assert_awaited_once_with(fake_session)


class GatewayLoopResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_consume_inbound_continues_after_processing_error(self) -> None:
        class _FakeRunner:
            async def run_async(self, **kwargs):
                if False:
                    yield  # pragma: no cover

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=bus)

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
    async def test_start_and_stop_manage_cron_and_heartbeat_service(self) -> None:
        class _FakeRunner:
            async def run_async(self, **kwargs):
                if False:
                    yield  # pragma: no cover

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        fake_cron_service = pytypes.SimpleNamespace(start=AsyncMock(), stop=Mock())
        fake_heartbeat_runner = pytypes.SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        heartbeat_waker = Mock()
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            with (
                patch("openpipixia.app.gateway.CronService", return_value=fake_cron_service),
                patch("openpipixia.app.gateway.HeartbeatRunner", return_value=fake_heartbeat_runner),
                patch("openpipixia.app.gateway.configure_heartbeat_waker", heartbeat_waker),
            ):
                gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())
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

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())

        req = HeartbeatRunRequest(reason="manual", prompt="ops check")
        await gateway._run_heartbeat(req)

        self.assertEqual(captured["user_id"], "heartbeat")
        self.assertEqual(captured["session_id"], "heartbeat:main")
        request = captured["new_message"]
        self.assertIn("ops check", request.parts[0].text)
        self.assertIn("Current time:", request.parts[0].text)

    async def test_run_heartbeat_skips_when_default_prompt_and_task_file_missing(self) -> None:
        called = False

        class _FakeRunner:
            async def run_async(self, **kwargs):
                nonlocal called
                called = True
                if False:
                    yield  # pragma: no cover

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())

        req = HeartbeatRunRequest(reason="interval", prompt=DEFAULT_HEARTBEAT_PROMPT)
        with tempfile.TemporaryDirectory() as tmp:
            policy = pytypes.SimpleNamespace(workspace_root=Path(tmp))
            with patch("openpipixia.app.gateway.load_security_policy", return_value=policy):
                await gateway._run_heartbeat(req)

        self.assertFalse(called)
        status = gateway.heartbeat_status()
        self.assertEqual(status["last_delivery"]["kind"], "task-missing")
        self.assertFalse(bool(status["last_delivery"]["delivered"]))

    async def test_run_heartbeat_skips_when_default_prompt_and_task_file_empty(self) -> None:
        called = False

        class _FakeRunner:
            async def run_async(self, **kwargs):
                nonlocal called
                called = True
                if False:
                    yield  # pragma: no cover

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())

        req = HeartbeatRunRequest(reason="interval", prompt=DEFAULT_HEARTBEAT_PROMPT)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "HEARTBEAT.md").write_text("\n  \n", encoding="utf-8")
            policy = pytypes.SimpleNamespace(workspace_root=workspace)
            with patch("openpipixia.app.gateway.load_security_policy", return_value=policy):
                await gateway._run_heartbeat(req)

        self.assertFalse(called)
        status = gateway.heartbeat_status()
        self.assertEqual(status["last_delivery"]["kind"], "task-empty")
        self.assertFalse(bool(status["last_delivery"]["delivered"]))

    async def test_run_heartbeat_skips_ok_delivery_when_show_ok_disabled(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="HEARTBEAT_OK")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=bus)

        req = HeartbeatRunRequest(reason="interval", prompt="ops check")
        with patch.dict(os.environ, {"OPENPIPIXIA_HEARTBEAT_SHOW_OK": "0"}, clear=False):
            await gateway._run_heartbeat(req)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), timeout=0.05)

    async def test_run_heartbeat_delivers_ok_when_show_ok_enabled(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="HEARTBEAT_OK")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=bus)

        req = HeartbeatRunRequest(reason="interval", prompt="ops check")
        with patch.dict(os.environ, {"OPENPIPIXIA_HEARTBEAT_SHOW_OK": "1"}, clear=False):
            await gateway._run_heartbeat(req)
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

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=bus)

        req = HeartbeatRunRequest(reason="interval", prompt="ops check")
        with patch.dict(
            os.environ,
            {
                "OPENPIPIXIA_HEARTBEAT_SHOW_OK": "0",
                "OPENPIPIXIA_HEARTBEAT_SHOW_ALERTS": "0",
                "OPENPIPIXIA_HEARTBEAT_ACK_MAX_CHARS": "0",
            },
            clear=False,
        ):
            await gateway._run_heartbeat(req)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), timeout=0.05)

        with patch.dict(
            os.environ,
            {
                "OPENPIPIXIA_HEARTBEAT_SHOW_OK": "0",
                "OPENPIPIXIA_HEARTBEAT_SHOW_ALERTS": "1",
                "OPENPIPIXIA_HEARTBEAT_ACK_MAX_CHARS": "0",
            },
            clear=False,
        ):
            await gateway._run_heartbeat(req)
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=0.2)
        self.assertEqual(outbound.content, "alert disk full")

    async def test_run_heartbeat_routes_to_last_inbound_target(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="alert disk full")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=bus)
        gateway._last_inbound_route = ("feishu", "chat-ops")

        req = HeartbeatRunRequest(reason="manual", prompt="ops check")
        with patch.dict(os.environ, {"OPENPIPIXIA_HEARTBEAT_TARGET": "last"}, clear=False):
            await gateway._run_heartbeat(req)

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

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=bus)

        req = HeartbeatRunRequest(reason="manual", prompt="ops check")
        with patch.dict(os.environ, {"OPENPIPIXIA_HEARTBEAT_TARGET": "none"}, clear=False):
            await gateway._run_heartbeat(req)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), timeout=0.05)

    async def test_run_heartbeat_target_channel_uses_explicit_channel_and_chat(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="HEARTBEAT_OK")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=bus)

        req = HeartbeatRunRequest(reason="interval", prompt="ops check")
        with patch.dict(
            os.environ,
            {
                "OPENPIPIXIA_HEARTBEAT_SHOW_OK": "1",
                "OPENPIPIXIA_HEARTBEAT_TARGET": "channel",
                "OPENPIPIXIA_HEARTBEAT_TARGET_CHANNEL": "slack",
                "OPENPIPIXIA_HEARTBEAT_TARGET_CHAT_ID": "C123",
            },
            clear=False,
        ):
            await gateway._run_heartbeat(req)
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

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())

        req = HeartbeatRunRequest(reason="manual", prompt="ops check")
        with patch.dict(
            os.environ,
            {
                "OPENPIPIXIA_HEARTBEAT_TARGET": "channel",
                "OPENPIPIXIA_HEARTBEAT_TARGET_CHANNEL": "feishu",
                "OPENPIPIXIA_HEARTBEAT_TARGET_CHAT_ID": "ops-room",
                "OPENPIPIXIA_HEARTBEAT_SHOW_ALERTS": "1",
                "OPENPIPIXIA_HEARTBEAT_ACK_MAX_CHARS": "0",
            },
            clear=False,
        ):
            await gateway._run_heartbeat(req)
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

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())

        req = HeartbeatRunRequest(reason="manual", prompt="ops check")
        with tempfile.TemporaryDirectory() as tmp:
            policy = pytypes.SimpleNamespace(workspace_root=Path(tmp))
            with patch("openpipixia.app.gateway.load_security_policy", return_value=policy):
                with patch.dict(os.environ, {"OPENPIPIXIA_HEARTBEAT_SHOW_OK": "1"}, clear=False):
                    await gateway._run_heartbeat(req)
                snapshot = read_heartbeat_status_snapshot(Path(tmp))

        self.assertIsNotNone(snapshot)
        self.assertTrue(bool(snapshot and snapshot.get("last_delivery", {}).get("delivered")))
        self.assertEqual(snapshot and snapshot.get("last_delivery", {}).get("kind"), "ok")

    async def test_heartbeat_status_before_start_has_safe_defaults(self) -> None:
        class _FakeRunner:
            async def run_async(self, **kwargs):
                if False:
                    yield  # pragma: no cover

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())

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

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=bus)

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

        started = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        finished = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        self.assertEqual(started.metadata.get("_tool_name"), "cron")
        self.assertEqual(outbound.channel, "local")
        self.assertEqual(outbound.chat_id, "c1")
        self.assertEqual(outbound.content, "cron answer")
        self.assertEqual(outbound.metadata.get("_event_class"), "step_output")
        self.assertEqual(finished.metadata.get("_step_phase"), "finished")

    async def test_run_cron_job_triggers_heartbeat_wake(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="cron answer")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())

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

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=MessageBus())

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

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=bus)

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

        started = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        completed = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        result = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        self.assertEqual(started.metadata.get("_step_phase"), "running")
        self.assertEqual(started.metadata.get("_step_kind"), "subagent")
        self.assertEqual(completed.channel, "local")
        self.assertEqual(completed.chat_id, "c1")
        self.assertEqual(completed.content, "parent resumed notify")
        self.assertEqual(completed.metadata.get("_event_class"), "step_update")
        self.assertEqual(completed.metadata.get("_step_phase"), "finished")
        self.assertEqual(completed.metadata.get("_tool_name"), "spawn_subagent")
        self.assertEqual(completed.metadata.get("_task_id"), "subagent-abc")
        self.assertEqual(result.metadata.get("_event_class"), "step_output")
        self.assertEqual(result.content, "background done")
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

        fake_agent = pytypes.SimpleNamespace(name="openpipixia")
        with patch("openpipixia.app.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="openpipixia", bus=bus)

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

        running = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        self.assertEqual(running.metadata.get("_step_phase"), "running")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_outbound(), timeout=0.05)

    def test_subagent_runner_uses_restricted_toolset_without_spawn(self) -> None:
        def read_stub(path: str) -> str:
            return path

        def spawn_subagent(prompt: str) -> dict[str, str]:
            return {"status": "pending", "task_id": "x"}

        root = LlmAgent(
            name="openpipixia",
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

        with patch("openpipixia.app.gateway.create_runner", side_effect=_create_runner_side_effect):
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
