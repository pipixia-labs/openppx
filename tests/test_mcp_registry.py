"""Tests for MCP toolset registry helpers."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool.mcp_session_manager import (
    SseConnectionParams,
    StdioConnectionParams,
    StreamableHTTPConnectionParams,
)

from openppx.core.mcp_registry import (
    _MCP_SERVERS_ENV,
    build_mcp_toolsets,
    build_mcp_toolsets_from_env,
    probe_mcp_toolsets,
    summarize_mcp_toolsets,
)
from openppx.runtime.mcp_proxy import McpLongTaskProxyTool


class FakeMcpTool(BaseTool):
    """Minimal ADK tool with MCP metadata for registry tests."""

    def __init__(self, *, name: str = "mcp_remote_echo") -> None:
        super().__init__(name=name, description="fake MCP tool")
        self._raw_mcp_tool = SimpleNamespace(name=name, inputSchema={})

    @property
    def raw_mcp_tool(self) -> Any:
        """Return raw MCP metadata like ADK McpTool."""
        return self._raw_mcp_tool

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        """Return the call arguments."""
        return {"args": args}


class McpRegistryTests(unittest.TestCase):
    def test_build_mcp_toolsets_stdio(self) -> None:
        toolsets = build_mcp_toolsets(
            {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                }
            }
        )
        self.assertEqual(len(toolsets), 1)
        self.assertIsInstance(toolsets[0]._connection_params, StdioConnectionParams)
        self.assertEqual(toolsets[0].tool_name_prefix, "mcp_filesystem")

    def test_build_mcp_toolsets_sse(self) -> None:
        toolsets = build_mcp_toolsets(
            {
                "remote": {
                    "url": "https://example.com/sse",
                }
            }
        )
        self.assertEqual(len(toolsets), 1)
        self.assertIsInstance(toolsets[0]._connection_params, SseConnectionParams)

    def test_build_mcp_toolsets_streamable_http(self) -> None:
        toolsets = build_mcp_toolsets(
            {
                "remote": {
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer t"},
                    "toolFilter": ["search"],
                    "toolNamePrefix": "x_",
                }
            }
        )
        self.assertEqual(len(toolsets), 1)
        self.assertIsInstance(toolsets[0]._connection_params, StreamableHTTPConnectionParams)
        self.assertEqual(toolsets[0].tool_name_prefix, "x")

    def test_build_mcp_toolsets_runtime_header_provider(self) -> None:
        toolsets = build_mcp_toolsets(
            {
                "remote": {
                    "url": "https://example.com/mcp",
                    "runtimeHeaders": {
                        "X-OpenPPX-User": "user_id",
                        "X-OpenPPX-Session": "session_id",
                        "X-OpenPPX-App": "app_name",
                        "X-OpenPPX-Invocation": "invocation_id",
                        "X-OpenPPX-Agent": "agent_name",
                        "X-OpenPPX-Request-Kind": "metadata.request_kind",
                        "X-OpenPPX-Tenant": "state.tenant_id",
                        "X-Static": "literal:fixed",
                    },
                }
            },
            log_registered=False,
        )

        toolset = toolsets[0]
        self.assertEqual(toolset.runtime_headers["X-OpenPPX-User"], "user_id")
        self.assertIsNotNone(toolset._header_provider)
        ctx = SimpleNamespace(
            user_id="user-1",
            invocation_id="inv-1",
            agent_name="openppx",
            session=SimpleNamespace(id="session-1", app_name="openppx"),
            run_config=SimpleNamespace(custom_metadata={"request_kind": "gateway_stream"}),
            state={"tenant_id": "tenant-1"},
        )

        headers = toolset._header_provider(ctx)  # type: ignore[misc]

        self.assertEqual(headers["X-OpenPPX-User"], "user-1")
        self.assertEqual(headers["X-OpenPPX-Session"], "session-1")
        self.assertEqual(headers["X-OpenPPX-App"], "openppx")
        self.assertEqual(headers["X-OpenPPX-Invocation"], "inv-1")
        self.assertEqual(headers["X-OpenPPX-Agent"], "openppx")
        self.assertEqual(headers["X-OpenPPX-Request-Kind"], "gateway_stream")
        self.assertEqual(headers["X-OpenPPX-Tenant"], "tenant-1")
        self.assertEqual(headers["X-Static"], "fixed")

    def test_build_mcp_toolsets_runtime_headers_default_disabled(self) -> None:
        toolsets = build_mcp_toolsets({"remote": {"url": "https://example.com/mcp"}}, log_registered=False)
        self.assertEqual(toolsets[0].runtime_headers, {})
        self.assertIsNone(toolsets[0]._header_provider)

    def test_build_mcp_toolsets_progress_events_default_disabled(self) -> None:
        toolsets = build_mcp_toolsets({"remote": {"url": "https://example.com/mcp"}}, log_registered=False)
        self.assertFalse(toolsets[0].progress_events)
        self.assertIsNone(toolsets[0]._progress_callback)

    def test_build_mcp_toolsets_long_task_proxy_default_enabled(self) -> None:
        toolsets = build_mcp_toolsets({"remote": {"url": "https://example.com/mcp"}}, log_registered=False)

        self.assertTrue(toolsets[0].long_task_proxy)
        self.assertEqual(toolsets[0].inline_budget_ms, 5000)

    def test_build_mcp_toolsets_long_task_proxy_can_be_disabled(self) -> None:
        toolsets = build_mcp_toolsets(
            {"remote": {"url": "https://example.com/mcp", "longTaskProxy": False, "inlineBudgetMs": 250}},
            log_registered=False,
        )

        self.assertFalse(toolsets[0].long_task_proxy)
        self.assertEqual(toolsets[0].inline_budget_ms, 250)

    def test_build_mcp_toolsets_job_protocol_config(self) -> None:
        toolsets = build_mcp_toolsets(
            {
                "remote": {
                    "url": "https://example.com/mcp",
                    "jobProtocol": {
                        "jobIdPath": "job.id",
                        "statusTool": "job_status",
                        "statusArgs": {"id": "{job_id}"},
                        "outputTool": "job_output",
                        "cancelTool": "job_cancel",
                        "pollTimeoutMs": 1500,
                    },
                }
            },
            log_registered=False,
        )

        protocol = toolsets[0].job_protocol
        self.assertIsNotNone(protocol)
        assert protocol is not None
        self.assertEqual(protocol.job_id_path, "job.id")
        self.assertEqual(protocol.status_tool, "job_status")
        self.assertEqual(protocol.status_args, {"id": "{job_id}"})
        self.assertEqual(protocol.output_tool, "job_output")
        self.assertEqual(protocol.cancel_tool, "job_cancel")
        self.assertEqual(protocol.poll_timeout_ms, 1500)

    def test_build_mcp_toolsets_from_env_invalid_json(self) -> None:
        with patch.dict(os.environ, {_MCP_SERVERS_ENV: "{bad json"}, clear=False):
            toolsets = build_mcp_toolsets_from_env()
        self.assertEqual(toolsets, [])

    def test_build_mcp_toolsets_from_env_skips_disabled_servers(self) -> None:
        with patch.dict(
            os.environ,
            {
                _MCP_SERVERS_ENV: (
                    '{"enabled_server":{"command":"python"},'
                    '"disabled_server":{"enabled":false,"command":"python"}}'
                )
            },
            clear=False,
        ):
            toolsets = build_mcp_toolsets_from_env(log_registered=False)
        self.assertEqual(len(toolsets), 1)
        self.assertEqual(toolsets[0].meta.name, "enabled_server")

    def test_build_mcp_toolsets_skips_invalid_server_config(self) -> None:
        toolsets = build_mcp_toolsets({"bad": "oops"})
        self.assertEqual(toolsets, [])

    def test_build_mcp_toolsets_skips_disabled_server(self) -> None:
        toolsets = build_mcp_toolsets(
            {
                "enabled_server": {
                    "command": "python",
                },
                "disabled_server": {
                    "enabled": False,
                    "command": "python",
                },
            }
        )
        self.assertEqual(len(toolsets), 1)
        self.assertEqual(toolsets[0].meta.name, "enabled_server")

    def test_build_mcp_toolsets_supports_string_enabled_flag(self) -> None:
        toolsets = build_mcp_toolsets(
            {
                "disabled_server": {
                    "enabled": "false",
                    "command": "python",
                }
            }
        )
        self.assertEqual(toolsets, [])

    def test_summarize_mcp_toolsets_returns_metadata(self) -> None:
        toolsets = build_mcp_toolsets(
            {
                "remote": {
                    "url": "https://example.com/sse",
                }
            },
            log_registered=False,
        )
        summaries = summarize_mcp_toolsets(toolsets)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["name"], "remote")
        self.assertEqual(summaries[0]["transport"], "sse")
        self.assertEqual(summaries[0]["prefix"], "mcp_remote")


class McpRegistryProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_mcp_toolset_wraps_mcp_tools_by_default(self) -> None:
        toolsets = build_mcp_toolsets({"remote": {"url": "https://example.com/mcp"}}, log_registered=False)
        fake_tool = FakeMcpTool()

        with patch("openppx.core.mcp_registry.McpToolset.get_tools", new=AsyncMock(return_value=[fake_tool])):
            tools = await toolsets[0].get_tools()

        self.assertEqual(len(tools), 1)
        self.assertIsInstance(tools[0], McpLongTaskProxyTool)
        self.assertIs(tools[0].wrapped_tool, fake_tool)
        self.assertEqual(tools[0].raw_mcp_tool.name, "mcp_remote_echo")

    async def test_managed_mcp_toolset_passes_job_protocol_to_wrapped_tools(self) -> None:
        toolsets = build_mcp_toolsets(
            {
                "remote": {
                    "url": "https://example.com/mcp",
                    "jobProtocol": {"jobIdPath": "job_id", "statusTool": "job_status"},
                }
            },
            log_registered=False,
        )
        fake_tool = FakeMcpTool()

        with patch("openppx.core.mcp_registry.McpToolset.get_tools", new=AsyncMock(return_value=[fake_tool])):
            tools = await toolsets[0].get_tools()

        metadata = tools[0].custom_metadata["openppx_mcp_proxy"]
        self.assertIsInstance(tools[0], McpLongTaskProxyTool)
        self.assertEqual(metadata["job_protocol"]["job_id_path"], "job_id")
        self.assertEqual(metadata["job_protocol"]["status_tool"], "job_status")

    async def test_managed_mcp_toolset_returns_raw_tools_when_proxy_disabled(self) -> None:
        toolsets = build_mcp_toolsets(
            {"remote": {"url": "https://example.com/mcp", "longTaskProxy": False}},
            log_registered=False,
        )
        fake_tool = FakeMcpTool()

        with patch("openppx.core.mcp_registry.McpToolset.get_tools", new=AsyncMock(return_value=[fake_tool])):
            tools = await toolsets[0].get_tools()

        self.assertEqual(tools, [fake_tool])

    async def test_progress_events_publish_step_update(self) -> None:
        toolsets = build_mcp_toolsets(
            {"remote": {"url": "https://example.com/mcp", "progressEvents": True}},
            log_registered=False,
        )
        toolset = toolsets[0]
        self.assertTrue(toolset.progress_events)
        self.assertIsNotNone(toolset._progress_callback)
        callback_context = SimpleNamespace(
            invocation_id="inv-1",
            function_call_id="call-1",
            session=SimpleNamespace(id="session-1"),
        )

        with patch("openppx.core.mcp_registry.publish_runtime_step_event", new=AsyncMock()) as mocked_publish:
            callback = toolset._progress_callback("mcp_remote_long_task", callback_context=callback_context)
            self.assertIsNotNone(callback)
            await callback(2, 4, "half done")

        mocked_publish.assert_awaited_once()
        kwargs = mocked_publish.await_args.kwargs
        self.assertEqual(kwargs["invocation_id"], "inv-1")
        self.assertEqual(kwargs["function_call_id"], "call-1")
        self.assertEqual(kwargs["step_phase"], "running")
        self.assertEqual(kwargs["step_update_kind"], "progress")
        self.assertEqual(kwargs["tool_name"], "mcp_remote_long_task")
        self.assertIn("50%", kwargs["content"])
        self.assertEqual(kwargs["extra_metadata"]["_mcp_server"], "remote")
        self.assertEqual(kwargs["extra_metadata"]["_mcp_progress"], 2)
        self.assertEqual(kwargs["extra_metadata"]["_mcp_total"], 4)

    async def test_safe_mcp_toolset_marks_unavailable_on_connection_error(self) -> None:
        toolsets = build_mcp_toolsets(
            {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}},
            log_registered=False,
        )
        toolset = toolsets[0]
        with patch("openppx.core.mcp_registry.McpToolset.get_tools", new=AsyncMock(side_effect=RuntimeError("boom"))):
            tools = await toolset.get_tools()
        self.assertEqual(tools, [])
        self.assertEqual(toolset.availability_status, "unavailable")
        self.assertIn("boom", toolset.availability_message)

    async def test_probe_mcp_toolsets_ok(self) -> None:
        toolsets = build_mcp_toolsets(
            {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}},
            log_registered=False,
        )
        with patch("openppx.core.mcp_registry.McpToolset.get_tools", new=AsyncMock(return_value=[object(), object()])):
            results = await probe_mcp_toolsets(toolsets, timeout_seconds=2.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["tool_count"], 2)
        self.assertEqual(results[0]["name"], "filesystem")

    async def test_probe_mcp_toolsets_error(self) -> None:
        toolsets = build_mcp_toolsets(
            {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}},
            log_registered=False,
        )
        with patch(
            "openppx.core.mcp_registry.McpToolset.get_tools",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            results = await probe_mcp_toolsets(toolsets, timeout_seconds=2.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("boom", results[0]["error"])
        self.assertIn("error_kind", results[0])
        self.assertIn("attempts", results[0])

    async def test_probe_mcp_toolsets_retries_transient_errors(self) -> None:
        toolsets = build_mcp_toolsets(
            {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}},
            log_registered=False,
        )
        side_effects = [RuntimeError("connection refused"), [object()]]
        with patch("openppx.core.mcp_registry.McpToolset.get_tools", new=AsyncMock(side_effect=side_effects)):
            with patch("openppx.core.mcp_registry.asyncio.sleep", new=AsyncMock()) as mocked_sleep:
                results = await probe_mcp_toolsets(
                    toolsets,
                    timeout_seconds=2.0,
                    retry_attempts=2,
                    retry_backoff_seconds=0.01,
                )
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["attempts"], 2)
        mocked_sleep.assert_awaited_once()

    async def test_probe_mcp_toolsets_config_errors_are_not_retried(self) -> None:
        toolsets = build_mcp_toolsets(
            {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}},
            log_registered=False,
        )
        with patch(
            "openppx.core.mcp_registry.McpToolset.get_tools",
            new=AsyncMock(side_effect=RuntimeError("invalid URL scheme")),
        ):
            with patch("openppx.core.mcp_registry.asyncio.sleep", new=AsyncMock()) as mocked_sleep:
                results = await probe_mcp_toolsets(
                    toolsets,
                    timeout_seconds=2.0,
                    retry_attempts=3,
                    retry_backoff_seconds=0.01,
                )
        self.assertEqual(results[0]["status"], "error")
        self.assertEqual(results[0]["error_kind"], "config")
        self.assertEqual(results[0]["attempts"], 1)
        mocked_sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
