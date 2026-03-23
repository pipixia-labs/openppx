"""Tests for MCP toolset wiring in root agent assembly."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class AgentMcpTests(unittest.TestCase):
    def test_build_tools_appends_mcp_toolsets(self) -> None:
        from openpipixia import agent

        sentinel_toolset = object()
        with patch("openpipixia.app.agent.build_mcp_toolsets_from_env", return_value=[sentinel_toolset]):
            tools = agent._build_tools()

        self.assertIn(sentinel_toolset, tools)

    def test_build_tools_keeps_builtin_gui_tools_enabled_by_default(self) -> None:
        from openpipixia import agent
        from openpipixia.tooling.registry import computer_task, computer_use

        with patch("openpipixia.app.agent.build_mcp_toolsets_from_env", return_value=[]):
            tools = agent._build_tools()
        self.assertIn(computer_task, tools)
        self.assertIn(computer_use, tools)

    def test_build_tools_can_disable_builtin_gui_tools(self) -> None:
        from openpipixia import agent
        from openpipixia.tooling.registry import computer_task, computer_use

        with patch.dict(os.environ, {"OPENPIPIXIA_GUI_BUILTIN_TOOLS_ENABLED": "0"}, clear=False):
            with patch("openpipixia.app.agent.build_mcp_toolsets_from_env", return_value=[]):
                tools = agent._build_tools()
        self.assertNotIn(computer_task, tools)
        self.assertNotIn(computer_use, tools)

    def test_build_instruction_uses_resolved_gui_mcp_tool_names(self) -> None:
        from openpipixia import agent

        with patch.dict(
            os.environ,
            {
                "OPENPIPIXIA_MCP_SERVERS_JSON": (
                    '{"gui_remote":{"enabled":true,"command":"openpipixia-gui-mcp","toolNamePrefix":"desktop_"}}'
                )
            },
            clear=False,
        ):
            text = agent._build_instruction()
        self.assertIn("desktop_gui_task", text)
        self.assertIn("desktop_gui_action", text)


if __name__ == "__main__":
    unittest.main()
