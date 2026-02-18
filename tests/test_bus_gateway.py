"""Tests for bus and gateway skeleton."""

from __future__ import annotations

import asyncio
import types as pytypes
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from google.adk.agents import LlmAgent
from google.adk.tools import LongRunningFunctionTool

from sentientagent_v2.bus.events import InboundMessage, OutboundMessage
from sentientagent_v2.bus.queue import MessageBus
from sentientagent_v2.gateway import Gateway
from sentientagent_v2.runtime.cron_service import CronJob, CronJobState, CronPayload, CronSchedule
from sentientagent_v2.tools import SubagentSpawnRequest


class MessageBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip(self) -> None:
        bus = MessageBus()
        inbound = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="ping")
        outbound = OutboundMessage(channel="local", chat_id="c1", content="pong")

        await bus.publish_inbound(inbound)
        await bus.publish_outbound(outbound)

        got_inbound = await bus.consume_inbound()
        got_outbound = await bus.consume_outbound()

        self.assertEqual(got_inbound.content, "ping")
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

        fake_agent = pytypes.SimpleNamespace(name="sentientagent_v2")
        with patch("sentientagent_v2.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="sentientagent_v2", bus=MessageBus())
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

        fake_agent = pytypes.SimpleNamespace(name="sentientagent_v2")
        with patch("sentientagent_v2.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="sentientagent_v2", bus=MessageBus())
            inbound = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="hello")
            outbound = asyncio.run(gateway.process_message(inbound))

        self.assertEqual(outbound.content, "hello world")


class GatewayLoopResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_consume_inbound_continues_after_processing_error(self) -> None:
        class _FakeRunner:
            async def run_async(self, **kwargs):
                if False:
                    yield  # pragma: no cover

        fake_agent = pytypes.SimpleNamespace(name="sentientagent_v2")
        with patch("sentientagent_v2.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="sentientagent_v2", bus=bus)

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
    async def test_start_and_stop_manage_cron_service(self) -> None:
        class _FakeRunner:
            async def run_async(self, **kwargs):
                if False:
                    yield  # pragma: no cover

        fake_agent = pytypes.SimpleNamespace(name="sentientagent_v2")
        fake_cron_service = pytypes.SimpleNamespace(start=AsyncMock(), stop=Mock())
        with patch("sentientagent_v2.gateway.create_runner", return_value=(_FakeRunner(), object())):
            with patch("sentientagent_v2.gateway.CronService", return_value=fake_cron_service):
                gateway = Gateway(agent=fake_agent, app_name="sentientagent_v2", bus=MessageBus())
                await gateway.start()
                fake_cron_service.start.assert_awaited_once()
                await gateway.stop()
                fake_cron_service.stop.assert_called_once()

    async def test_run_cron_job_delivers_outbound_when_enabled(self) -> None:
        fake_event = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="cron answer")])
        )
        captured: dict[str, object] = {}

        class _FakeRunner:
            async def run_async(self, **kwargs):
                captured.update(kwargs)
                yield fake_event

        fake_agent = pytypes.SimpleNamespace(name="sentientagent_v2")
        with patch("sentientagent_v2.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="sentientagent_v2", bus=bus)

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

        fake_agent = pytypes.SimpleNamespace(name="sentientagent_v2")
        with patch("sentientagent_v2.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="sentientagent_v2", bus=bus)

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

        fake_agent = pytypes.SimpleNamespace(name="sentientagent_v2")
        with patch("sentientagent_v2.gateway.create_runner", return_value=(_FakeRunner(), object())):
            bus = MessageBus()
            gateway = Gateway(agent=fake_agent, app_name="sentientagent_v2", bus=bus)

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
            name="sentientagent_v2",
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

        with patch("sentientagent_v2.gateway.create_runner", side_effect=_create_runner_side_effect):
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
