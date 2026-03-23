"""Tests for GUI MCP routing helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from openpipixia.core.gui_mcp import resolve_gui_mcp_from_env, resolve_gui_mcp_from_summaries


class GuiMcpRoutingTests(unittest.TestCase):
    def test_resolve_gui_mcp_from_env_with_default_server_name(self) -> None:
        raw = '{"openpipixia_gui":{"enabled":true,"command":"openpipixia-gui-mcp"}}'
        with patch.dict(os.environ, {"OPENPIPIXIA_MCP_SERVERS_JSON": raw}, clear=False):
            routing = resolve_gui_mcp_from_env()
        self.assertIsNotNone(routing)
        assert routing is not None
        self.assertEqual(routing.tool_prefix, "mcp_openpipixia_gui_")
        self.assertEqual(routing.task_tool_name, "mcp_openpipixia_gui_gui_task")
        self.assertEqual(routing.action_tool_name, "mcp_openpipixia_gui_gui_action")

    def test_resolve_gui_mcp_from_env_with_custom_prefix(self) -> None:
        raw = '{"gui_remote":{"enabled":true,"command":"openpipixia-gui-mcp","toolNamePrefix":"desktop_"}}'
        with patch.dict(os.environ, {"OPENPIPIXIA_MCP_SERVERS_JSON": raw}, clear=False):
            routing = resolve_gui_mcp_from_env()
        self.assertIsNotNone(routing)
        assert routing is not None
        self.assertEqual(routing.task_tool_name, "desktop_gui_task")
        self.assertEqual(routing.action_tool_name, "desktop_gui_action")

    def test_resolve_gui_mcp_from_env_handles_python_module_command(self) -> None:
        raw = (
            '{"remote":{"enabled":true,"command":"python",'
            '"args":["-m","openpipixia.gui.mcp_server"],"toolNamePrefix":"x_"}}'
        )
        with patch.dict(os.environ, {"OPENPIPIXIA_MCP_SERVERS_JSON": raw}, clear=False):
            routing = resolve_gui_mcp_from_env()
        self.assertIsNotNone(routing)
        assert routing is not None
        self.assertEqual(routing.tool_prefix, "x_")

    def test_resolve_gui_mcp_from_summaries_fallback(self) -> None:
        routing = resolve_gui_mcp_from_summaries(
            [{"name": "custom", "prefix": "mcp_gui_", "transport": "stdio"}]
        )
        self.assertIsNotNone(routing)
        assert routing is not None
        self.assertEqual(routing.task_tool_name, "mcp_gui_gui_task")


if __name__ == "__main__":
    unittest.main()
