"""Tests for built-in GUI MCP server wrappers."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from openpipixia.gui.mcp_server import (
    build_gui_mcp_server,
    main,
    run_gui_action,
    run_gui_task,
)


class GuiMcpServerTests(unittest.TestCase):
    def test_run_gui_action_requires_action(self) -> None:
        result = run_gui_action(action="  ")
        self.assertEqual(result["ok"], False)
        self.assertIn("required", result["error"])

    def test_run_gui_action_delegates(self) -> None:
        expected = {"ok": True, "action": "left_click"}
        with patch("openpipixia.gui.mcp_server.execute_gui_action", return_value=expected) as mocked:
            result = run_gui_action(action=" click search box ", dry_run=True)

        self.assertEqual(result, expected)
        mocked.assert_called_once_with(
            action="click search box",
            dry_run=True,
            model=None,
            api_key=None,
            base_url=None,
        )

    def test_run_gui_action_wraps_exceptions(self) -> None:
        with patch("openpipixia.gui.mcp_server.execute_gui_action", side_effect=RuntimeError("boom")):
            result = run_gui_action(action="click")
        self.assertEqual(result["ok"], False)
        self.assertIn("boom", result["error"])

    def test_run_gui_task_requires_task(self) -> None:
        result = run_gui_task(task="")
        self.assertEqual(result["ok"], False)
        self.assertIn("required", result["error"])

    def test_run_gui_task_delegates(self) -> None:
        expected = {"ok": True, "finished": False}
        with patch("openpipixia.gui.mcp_server.execute_gui_task", return_value=expected) as mocked:
            result = run_gui_task(task="open browser", max_steps=5, dry_run=True)

        self.assertEqual(result, expected)
        mocked.assert_called_once_with(
            task="open browser",
            max_steps=5,
            dry_run=True,
            planner_model=None,
            planner_api_key=None,
            planner_base_url=None,
        )

    def test_build_gui_mcp_server_registers_tools(self) -> None:
        server = build_gui_mcp_server()
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        self.assertIn("gui_action", names)
        self.assertIn("gui_task", names)

    def test_main_raises_for_invalid_transport(self) -> None:
        with patch.dict(os.environ, {"OPENPIPIXIA_GUI_MCP_TRANSPORT": "bad"}, clear=False):
            with self.assertRaises(ValueError):
                main()

    def test_main_runs_server(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENPIPIXIA_GUI_MCP_NAME": "gui-server",
                "OPENPIPIXIA_GUI_MCP_TRANSPORT": "stdio",
            },
            clear=False,
        ):
            with patch("openpipixia.gui.mcp_server.build_gui_mcp_server") as mocked_builder:
                main()

        mocked_builder.assert_called_once_with(name="gui-server")
        mocked_builder.return_value.run.assert_called_once_with(transport="stdio")


if __name__ == "__main__":
    unittest.main()
