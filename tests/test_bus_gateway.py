"""Tests for bus and gateway skeleton."""

from __future__ import annotations

import asyncio
import types as pytypes
import unittest
from unittest.mock import AsyncMock, patch

from sentientagent_v2.bus.events import InboundMessage, OutboundMessage
from sentientagent_v2.bus.queue import MessageBus
from sentientagent_v2.gateway import Gateway


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

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event_1
                yield fake_event_2

        fake_agent = pytypes.SimpleNamespace(name="sentientagent_v2")
        with patch("sentientagent_v2.gateway.create_runner", return_value=(_FakeRunner(), object())):
            gateway = Gateway(agent=fake_agent, app_name="sentientagent_v2", bus=MessageBus())
            inbound = InboundMessage(channel="local", sender_id="u1", chat_id="c1", content="hello")
            outbound = asyncio.run(gateway.process_message(inbound))

        self.assertEqual(outbound.channel, "local")
        self.assertEqual(outbound.chat_id, "c1")
        self.assertEqual(outbound.content, "gateway answer")

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


if __name__ == "__main__":
    unittest.main()
