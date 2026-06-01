"""Tests for MCP toolset wiring in root agent assembly."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


def _tool_names(tools: list[object]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        if hasattr(tool, "name") and isinstance(getattr(tool, "name"), str):
            names.add(getattr(tool, "name"))
            continue
        if hasattr(tool, "func"):
            names.add(getattr(getattr(tool, "func"), "__name__", str(tool)))
            continue
        names.add(getattr(tool, "__name__", str(tool)))
    return names


class AgentMcpTests(unittest.TestCase):
    def test_build_tools_appends_mcp_toolsets(self) -> None:
        from openppx import agent

        sentinel_toolset = object()
        with patch("openppx.app.agent.build_mcp_toolsets_from_env", return_value=[sentinel_toolset]):
            tools = agent._build_tools()

        self.assertIn(sentinel_toolset, tools)

    def test_build_tools_keeps_builtin_gui_tools_enabled_by_default(self) -> None:
        from openppx import agent
        from openppx.tooling.registry import computer_task, computer_use, glob, grep

        with patch("openppx.app.agent.build_mcp_toolsets_from_env", return_value=[]):
            tools = agent._build_tools()
        self.assertIn(computer_task, tools)
        self.assertIn(computer_use, tools)
        self.assertIn(glob, tools)
        self.assertIn(grep, tools)

    def test_build_tools_can_disable_builtin_gui_tools(self) -> None:
        from openppx import agent
        from openppx.tooling.registry import computer_task, computer_use

        with patch.dict(os.environ, {"OPENPPX_GUI_BUILTIN_TOOLS_ENABLED": "0"}, clear=False):
            with patch("openppx.app.agent.build_mcp_toolsets_from_env", return_value=[]):
                tools = agent._build_tools()
        self.assertNotIn(computer_task, tools)
        self.assertNotIn(computer_use, tools)

    def test_build_instruction_uses_resolved_gui_mcp_tool_names(self) -> None:
        from openppx import agent

        with patch.dict(
            os.environ,
            {
                "OPENPPX_MCP_SERVERS_JSON": (
                    '{"gui_remote":{"enabled":true,"command":"openppx-gui-mcp","toolNamePrefix":"desktop_"}}'
                )
            },
            clear=False,
        ):
            text = agent._build_instruction()
        self.assertIn("desktop_gui_task", text)
        self.assertIn("desktop_gui_action", text)

    def test_build_tools_limits_low_to_read_only_tools(self) -> None:
        from openppx import agent
        from openppx.tooling.registry import read_file, list_dir, write_file, exec_command, web_search

        with patch.dict(
            os.environ,
            {"OPENPPX_AGENT_PRIVILEGE_LEVEL": "low", "OPENPPX_CAN_DELEGATE": "0"},
            clear=False,
        ):
            with patch("openppx.app.agent.build_mcp_toolsets_from_env", return_value=[]):
                tools = agent._build_tools()

        self.assertIn(read_file, tools)
        self.assertIn(list_dir, tools)
        self.assertNotIn(write_file, tools)
        self.assertNotIn(exec_command, tools)
        self.assertNotIn(web_search, tools)

    def test_build_tools_keeps_medium_exec_and_web_but_hides_message_tools(self) -> None:
        from openppx import agent
        from openppx.tooling.registry import exec_command, web_search, message, message_file

        with patch.dict(
            os.environ,
            {"OPENPPX_AGENT_PRIVILEGE_LEVEL": "medium", "OPENPPX_CAN_DELEGATE": "1"},
            clear=False,
        ):
            with patch("openppx.app.agent.build_mcp_toolsets_from_env", return_value=[]):
                tools = agent._build_tools()

        names = _tool_names(tools)
        self.assertIn("exec", names)
        self.assertIn("web_search", names)
        self.assertNotIn("message", names)
        self.assertNotIn("message_file", names)

    def test_build_tools_keeps_high_full_tool_access(self) -> None:
        from openppx import agent
        from openppx.tooling.registry import exec_command, web_search, message, message_file

        with patch.dict(
            os.environ,
            {"OPENPPX_AGENT_PRIVILEGE_LEVEL": "high", "OPENPPX_CAN_DELEGATE": "1"},
            clear=False,
        ):
            with patch("openppx.app.agent.build_mcp_toolsets_from_env", return_value=[]):
                tools = agent._build_tools()

        names = _tool_names(tools)
        self.assertIn("exec", names)
        self.assertIn("web_search", names)
        self.assertIn("message", names)
        self.assertIn("message_file", names)


if __name__ == "__main__":
    unittest.main()
