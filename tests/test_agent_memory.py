"""Tests for ADK memory wiring in root agent."""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from google.adk.tools import load_artifacts
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from openppx.runtime.interaction_context import (
    INTERACTION_CONTEXT_STATE_KEY,
    MEMORY_INGEST_OFFSET_STATE_KEY,
)
from openppx.runtime.memory_ingest_plugin import OpenPpxMemoryIngestPlugin


class AgentMemoryTests(unittest.TestCase):
    def test_build_tools_includes_preload_memory_tool(self) -> None:
        from openppx import agent

        tools = agent._build_tools()
        self.assertTrue(any(isinstance(item, PreloadMemoryTool) for item in tools))

    def test_build_tools_includes_load_artifacts(self) -> None:
        from openppx import agent

        tools = agent._build_tools()
        self.assertIn(load_artifacts, tools)

    def test_before_agent_memory_callback_sets_fallback_offset(self) -> None:
        plugin = OpenPpxMemoryIngestPlugin(target_agent_name="openppx")
        adk_agent = types.SimpleNamespace(name="openppx")
        callback_context = types.SimpleNamespace(
            state={},
            session=types.SimpleNamespace(events=[object(), object(), object()]),
        )

        asyncio.run(plugin.before_agent_callback(agent=adk_agent, callback_context=callback_context))

        self.assertEqual(callback_context.state[MEMORY_INGEST_OFFSET_STATE_KEY], 3)

    def test_after_agent_memory_callback_persists_new_events(self) -> None:
        plugin = OpenPpxMemoryIngestPlugin(target_agent_name="openppx")
        adk_agent = types.SimpleNamespace(name="openppx")
        event_1 = object()
        event_2 = object()
        callback_context = types.SimpleNamespace(
            state={
                MEMORY_INGEST_OFFSET_STATE_KEY: 1,
                INTERACTION_CONTEXT_STATE_KEY: {
                    "requester_principal_id": "u1",
                    "memory_ingest_enabled": True,
                },
            },
            session=types.SimpleNamespace(events=[object(), event_1, event_2]),
            add_events_to_memory=AsyncMock(return_value=None),
        )

        asyncio.run(plugin.after_agent_callback(agent=adk_agent, callback_context=callback_context))

        callback_context.add_events_to_memory.assert_awaited_once()
        kwargs = callback_context.add_events_to_memory.await_args.kwargs
        self.assertEqual(kwargs["events"], [event_1, event_2])
        self.assertEqual(kwargs["custom_metadata"]["requester_principal_id"], "u1")
        self.assertEqual(kwargs["custom_metadata"]["ingest_reason"], "after_agent_callback")

    def test_after_agent_memory_callback_skips_silent_service_principal(self) -> None:
        plugin = OpenPpxMemoryIngestPlugin(target_agent_name="openppx")
        adk_agent = types.SimpleNamespace(name="openppx")
        callback_context = types.SimpleNamespace(
            state={
                MEMORY_INGEST_OFFSET_STATE_KEY: 0,
                INTERACTION_CONTEXT_STATE_KEY: {
                    "requester_principal_id": "heartbeat",
                    "memory_ingest_enabled": False,
                },
            },
            session=types.SimpleNamespace(events=[object()]),
            add_events_to_memory=AsyncMock(return_value=None),
        )

        asyncio.run(plugin.after_agent_callback(agent=adk_agent, callback_context=callback_context))

        callback_context.add_events_to_memory.assert_not_awaited()

    def test_after_agent_memory_callback_ignores_missing_memory_service(self) -> None:
        plugin = OpenPpxMemoryIngestPlugin(target_agent_name="openppx")
        adk_agent = types.SimpleNamespace(name="openppx")
        callback_context = types.SimpleNamespace(
            state={MEMORY_INGEST_OFFSET_STATE_KEY: 0},
            session=types.SimpleNamespace(events=[object()]),
            add_events_to_memory=AsyncMock(side_effect=ValueError("memory service is not available")),
        )
        asyncio.run(plugin.after_agent_callback(agent=adk_agent, callback_context=callback_context))
        callback_context.add_events_to_memory.assert_awaited_once()

    def test_root_agent_leaves_runtime_callbacks_to_app_plugins(self) -> None:
        from openppx import agent

        self.assertIsNone(agent.root_agent.before_agent_callback)
        self.assertIsNone(agent.root_agent.after_agent_callback)
        self.assertIsNone(agent.root_agent.before_model_callback)
        self.assertIsNone(agent.root_agent.after_model_callback)

    def test_mcp_toolsets_still_appended_after_memory_tool(self) -> None:
        from openppx import agent

        sentinel_toolset = object()
        with patch("openppx.app.agent.build_mcp_toolsets_from_env", return_value=[sentinel_toolset]):
            tools = agent._build_tools()
        self.assertIn(sentinel_toolset, tools)


if __name__ == "__main__":
    unittest.main()
