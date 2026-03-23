"""Tests for MCP toolset registry helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from google.adk.tools.mcp_tool.mcp_session_manager import (
    SseConnectionParams,
    StdioConnectionParams,
    StreamableHTTPConnectionParams,
)

from openpipixia.core.mcp_registry import (
    _MCP_SERVERS_ENV,
    build_mcp_toolsets,
    build_mcp_toolsets_from_env,
    probe_mcp_toolsets,
    summarize_mcp_toolsets,
)


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
        self.assertEqual(toolsets[0].tool_name_prefix, "mcp_filesystem_")

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
        self.assertEqual(toolsets[0].tool_name_prefix, "x_")

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
        self.assertEqual(summaries[0]["prefix"], "mcp_remote_")


class McpRegistryProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_mcp_toolset_marks_unavailable_on_connection_error(self) -> None:
        toolsets = build_mcp_toolsets(
            {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}},
            log_registered=False,
        )
        toolset = toolsets[0]
        with patch("openpipixia.core.mcp_registry.McpToolset.get_tools", new=AsyncMock(side_effect=RuntimeError("boom"))):
            tools = await toolset.get_tools()
        self.assertEqual(tools, [])
        self.assertEqual(toolset.availability_status, "unavailable")
        self.assertIn("boom", toolset.availability_message)

    async def test_probe_mcp_toolsets_ok(self) -> None:
        toolsets = build_mcp_toolsets(
            {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}},
            log_registered=False,
        )
        with patch("openpipixia.core.mcp_registry.McpToolset.get_tools", new=AsyncMock(return_value=[object(), object()])):
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
            "openpipixia.core.mcp_registry.McpToolset.get_tools",
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
        with patch("openpipixia.core.mcp_registry.McpToolset.get_tools", new=AsyncMock(side_effect=side_effects)):
            with patch("openpipixia.core.mcp_registry.asyncio.sleep", new=AsyncMock()) as mocked_sleep:
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
            "openpipixia.core.mcp_registry.McpToolset.get_tools",
            new=AsyncMock(side_effect=RuntimeError("invalid URL scheme")),
        ):
            with patch("openpipixia.core.mcp_registry.asyncio.sleep", new=AsyncMock()) as mocked_sleep:
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
