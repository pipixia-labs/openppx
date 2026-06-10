"""Tests for built-in GUI MCP server wrappers."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from openppx.gui.mcp_server import (
    add_agent_participant,
    build_gui_mcp_server,
    cancel_gui_task,
    get_agent_access,
    get_gui_task_output,
    get_gui_task_status,
    list_agent_access_audit,
    list_agent_memory_audit,
    main,
    remove_agent_participant,
    resume_gui_task,
    run_gui_action,
    run_gui_task,
    set_agent_owner,
    submit_gui_task,
)


class GuiMcpServerTests(unittest.TestCase):
    def test_run_gui_action_requires_action(self) -> None:
        result = run_gui_action(action="  ")
        self.assertEqual(result["ok"], False)
        self.assertIn("required", result["error"])

    def test_run_gui_action_delegates(self) -> None:
        expected = {"ok": True, "action": "left_click"}
        with patch("openppx.gui.mcp_server.execute_gui_action", return_value=expected) as mocked:
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
        with patch("openppx.gui.mcp_server.execute_gui_action", side_effect=RuntimeError("boom")):
            result = run_gui_action(action="click")
        self.assertEqual(result["ok"], False)
        self.assertIn("boom", result["error"])

    def test_run_gui_task_requires_task(self) -> None:
        result = run_gui_task(task="")
        self.assertEqual(result["ok"], False)
        self.assertIn("required", result["error"])

    def test_run_gui_task_delegates(self) -> None:
        expected = {"ok": True, "finished": False}
        with patch("openppx.gui.mcp_server.execute_gui_task", return_value=expected) as mocked:
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

    def test_submit_gui_task_delegates_to_job_coordinator(self) -> None:
        expected = {"ok": True, "job_id": "gui_job_1", "status": "running"}
        with patch("openppx.gui.mcp_server.submit_gui_task_job", return_value=expected) as mocked:
            result = submit_gui_task(task="open browser", max_steps=5, dry_run=True)

        self.assertEqual(result, expected)
        mocked.assert_called_once_with(
            task="open browser",
            max_steps=5,
            dry_run=True,
            planner_model=None,
            planner_api_key=None,
            planner_base_url=None,
        )

    def test_gui_task_job_wrappers_require_job_id(self) -> None:
        self.assertFalse(get_gui_task_status(job_id=" ")["ok"])
        self.assertFalse(get_gui_task_output(job_id=" ")["ok"])
        self.assertFalse(cancel_gui_task(job_id=" ")["ok"])

    def test_gui_task_job_wrappers_delegate(self) -> None:
        with patch("openppx.gui.mcp_server.gui_task_job_status", return_value={"ok": True, "status": "running"}) as status:
            self.assertEqual(get_gui_task_status(job_id="gui_job_1")["status"], "running")
        with patch("openppx.gui.mcp_server.gui_task_job_output", return_value={"ok": True, "output": "done"}) as output:
            self.assertEqual(get_gui_task_output(job_id="gui_job_1")["output"], "done")
        with patch("openppx.gui.mcp_server.gui_task_job_cancel", return_value={"ok": True, "action": "cancelled"}) as cancel:
            self.assertEqual(cancel_gui_task(job_id="gui_job_1", terminal_status="cancelled")["action"], "cancelled")

        status.assert_called_once_with("gui_job_1")
        output.assert_called_once_with("gui_job_1")
        cancel.assert_called_once_with("gui_job_1", terminal_status="cancelled", reason="")

    def test_resume_gui_task_uses_latest_checkpoint_from_job(self) -> None:
        checkpoint = {"task": "continue", "history": [{"step": 1}]}
        with patch(
            "openppx.gui.mcp_server.gui_task_job_status",
            return_value={"ok": True, "checkpoint": checkpoint},
        ) as status:
            with patch(
                "openppx.gui.mcp_server.resume_gui_task_job",
                return_value={"ok": True, "job_id": "gui_job_2"},
            ) as resume:
                result = resume_gui_task(job_id="gui_job_1")

        self.assertTrue(result["ok"])
        self.assertEqual(result["job_id"], "gui_job_2")
        status.assert_called_once_with("gui_job_1")
        resume.assert_called_once_with(checkpoint=checkpoint)

    def test_get_agent_access_requires_agent_id(self) -> None:
        result = get_agent_access(agent_id=" ")
        self.assertEqual(result["ok"], False)
        self.assertIn("agent_id", result["error"])

    def test_get_agent_access_delegates(self) -> None:
        expected = {"ok": True, "data": {"agent": {"id": "writer"}}}
        mocked_coordinator = unittest.mock.Mock()
        mocked_coordinator.get_agent_access.return_value = expected
        with patch("openppx.gui.mcp_server._coordinator_for_data_dir", return_value=mocked_coordinator):
            result = get_agent_access(agent_id="writer", user_id="owner", data_dir="/tmp/demo")

        self.assertEqual(result, expected)
        mocked_coordinator.get_agent_access.assert_called_once_with("writer", user_id="owner")

    def test_set_agent_owner_delegates(self) -> None:
        expected = {"ok": True}
        mocked_coordinator = unittest.mock.Mock()
        mocked_coordinator.set_agent_owner.return_value = expected
        with patch("openppx.gui.mcp_server._coordinator_for_data_dir", return_value=mocked_coordinator):
            result = set_agent_owner(agent_id="writer", owner_principal_id="root-user", user_id="root-user")

        self.assertEqual(result, expected)
        mocked_coordinator.set_agent_owner.assert_called_once_with("writer", "root-user", user_id="root-user")

    def test_list_agent_memory_audit_delegates(self) -> None:
        expected = {"ok": True, "data": {"items": []}}
        mocked_coordinator = unittest.mock.Mock()
        mocked_coordinator.get_memory_audit.return_value = expected
        with patch("openppx.gui.mcp_server._coordinator_for_data_dir", return_value=mocked_coordinator):
            result = list_agent_memory_audit(agent_id="writer", user_id="owner", limit=25)

        self.assertEqual(result, expected)
        mocked_coordinator.get_memory_audit.assert_called_once_with("writer", user_id="owner", limit=25)

    def test_list_agent_access_audit_delegates(self) -> None:
        expected = {"ok": True, "data": {"items": []}}
        mocked_coordinator = unittest.mock.Mock()
        mocked_coordinator.get_access_audit.return_value = expected
        with patch("openppx.gui.mcp_server._coordinator_for_data_dir", return_value=mocked_coordinator):
            result = list_agent_access_audit(agent_id="writer", user_id="owner", limit=10)

        self.assertEqual(result, expected)
        mocked_coordinator.get_access_audit.assert_called_once_with("writer", user_id="owner", limit=10)

    def test_add_and_remove_agent_participant_delegate(self) -> None:
        mocked_coordinator = unittest.mock.Mock()
        mocked_coordinator.upsert_agent_membership.return_value = {"ok": True, "data": {"membership": {}}}
        mocked_coordinator.delete_agent_membership.return_value = {"ok": True, "data": {"deleted": True}}
        with patch("openppx.gui.mcp_server._coordinator_for_data_dir", return_value=mocked_coordinator):
            add_result = add_agent_participant(agent_id="writer", principal_id="alice", user_id="owner")
            remove_result = remove_agent_participant(agent_id="writer", principal_id="alice", user_id="owner")

        self.assertEqual(add_result["ok"], True)
        self.assertEqual(remove_result["ok"], True)
        mocked_coordinator.upsert_agent_membership.assert_called_once_with(
            "writer",
            "alice",
            relation="participant",
            user_id="owner",
        )
        mocked_coordinator.delete_agent_membership.assert_called_once_with(
            "writer",
            "alice",
            user_id="owner",
        )

    def test_build_gui_mcp_server_registers_tools(self) -> None:
        server = build_gui_mcp_server()
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        self.assertIn("gui_action", names)
        self.assertIn("gui_task", names)
        self.assertIn("gui_task_submit", names)
        self.assertIn("gui_task_status", names)
        self.assertIn("gui_task_output", names)
        self.assertIn("gui_task_cancel", names)
        self.assertIn("gui_task_resume", names)
        self.assertIn("agent_access_get", names)
        self.assertIn("agent_access_audit_list", names)
        self.assertIn("agent_memory_audit_list", names)
        self.assertIn("agent_owner_set", names)
        self.assertIn("agent_participant_add", names)
        self.assertIn("agent_participant_remove", names)

    def test_main_raises_for_invalid_transport(self) -> None:
        with patch.dict(os.environ, {"OPENPPX_GUI_MCP_TRANSPORT": "bad"}, clear=False):
            with self.assertRaises(ValueError):
                main()

    def test_main_runs_server(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENPPX_GUI_MCP_NAME": "gui-server",
                "OPENPPX_GUI_MCP_TRANSPORT": "stdio",
            },
            clear=False,
        ):
            with patch("openppx.gui.mcp_server.build_gui_mcp_server") as mocked_builder:
                main()

        mocked_builder.assert_called_once_with(name="gui-server")
        mocked_builder.return_value.run.assert_called_once_with(transport="stdio")


if __name__ == "__main__":
    unittest.main()
