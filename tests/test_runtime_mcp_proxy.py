"""Tests for MCP long-task proxy runtime behavior."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from google.adk.tools.base_tool import BaseTool

from openppx.runtime.mcp_job_protocol import McpJobProtocolConfig
from openppx.runtime.mcp_job_protocol import clear_mcp_job_tools
from openppx.runtime.mcp_job_protocol import register_mcp_job_tools
from openppx.runtime.mcp_proxy import run_mcp_tool_with_proxy
from openppx.runtime.task_execution import TaskController
from openppx.runtime.task_store import TaskStore


class FakeMcpRuntimeTool(BaseTool):
    """Minimal ADK MCP-like tool for proxy runtime tests."""

    def __init__(
        self,
        *,
        result: Any,
        delay_seconds: float = 0.0,
        name: str = "mcp_remote_echo",
    ) -> None:
        super().__init__(name=name, description="fake MCP runtime tool")
        self._result = result
        self._delay_seconds = delay_seconds
        self._raw_mcp_tool = SimpleNamespace(name=name, inputSchema={})

    @property
    def raw_mcp_tool(self) -> Any:
        """Return raw MCP metadata like ADK McpTool."""
        return self._raw_mcp_tool

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        """Return the configured result after an optional delay."""
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        return self._result


def _tool_context() -> Any:
    """Return a minimal ADK ToolContext-like object."""
    return SimpleNamespace(
        user_id="user-1",
        invocation_id="inv-1",
        function_call_id="call-1",
        session=SimpleNamespace(id="session-1", user_id="user-1"),
    )


class McpProxyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "tasks.db"
        os.environ["OPENPPX_TASK_DB_PATH"] = str(self.db_path)

    def tearDown(self) -> None:
        clear_mcp_job_tools()
        self._tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(self._env_backup)

    async def test_fast_mcp_call_returns_inline_without_task(self) -> None:
        tool = FakeMcpRuntimeTool(result={"ok": True, "value": 7})

        result = await run_mcp_tool_with_proxy(
            wrapped_tool=tool,
            server_name="remote",
            transport="http",
            args={"value": 7},
            tool_context=_tool_context(),
            inline_budget_ms=1000,
        )

        self.assertEqual(result, {"ok": True, "value": 7})
        self.assertEqual(TaskStore(db_path=self.db_path).list_tasks(), [])

    async def test_slow_mcp_call_materializes_task_and_completes_in_background(self) -> None:
        tool = FakeMcpRuntimeTool(result={"ok": True, "body": "done"}, delay_seconds=0.05)

        payload = await run_mcp_tool_with_proxy(
            wrapped_tool=tool,
            server_name="remote",
            transport="http",
            args={"query": "x"},
            tool_context=_tool_context(),
            inline_budget_ms=0,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "task")
        task_id = payload["task_id"]
        store = TaskStore(db_path=self.db_path)
        task = store.get_task(task_id)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "running")
        self.assertEqual(task.runner_payload["runner"], "mcp_proxy")
        self.assertEqual(task.runner_payload["server"], "remote")

        completed = None
        for _ in range(30):
            await asyncio.sleep(0.02)
            completed = store.get_task(task_id)
            if completed is not None and completed.status == "completed":
                break

        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(completed.status, "completed")
        self.assertIn("done", completed.terminal_summary)
        output = TaskController(task_store=store).task_output(task_id)
        self.assertEqual(output["status"], "completed")
        self.assertIn("done", output["output"])

    async def test_background_mcp_error_result_marks_task_failed(self) -> None:
        tool = FakeMcpRuntimeTool(
            result={"isError": True, "message": "remote failed"},
            delay_seconds=0.05,
        )

        payload = await run_mcp_tool_with_proxy(
            wrapped_tool=tool,
            server_name="remote",
            transport="http",
            args={},
            tool_context=_tool_context(),
            inline_budget_ms=0,
        )

        store = TaskStore(db_path=self.db_path)
        failed = None
        for _ in range(30):
            await asyncio.sleep(0.02)
            failed = store.get_task(payload["task_id"])
            if failed is not None and failed.status == "failed":
                break

        self.assertIsNotNone(failed)
        assert failed is not None
        self.assertEqual(failed.status, "failed")
        self.assertIn("remote failed", failed.last_error)

    async def test_slow_mcp_call_can_be_interrupted_while_attached(self) -> None:
        tool = FakeMcpRuntimeTool(result={"ok": True}, delay_seconds=10.0)

        payload = await run_mcp_tool_with_proxy(
            wrapped_tool=tool,
            server_name="remote",
            transport="http",
            args={},
            tool_context=_tool_context(),
            inline_budget_ms=0,
        )

        store = TaskStore(db_path=self.db_path)
        controller = TaskController(task_store=store)
        shown = controller.show_task(payload["task_id"])
        interrupted = controller.interrupt_task(payload["task_id"])
        await asyncio.sleep(0)
        task = store.get_task(payload["task_id"])

        self.assertTrue(shown["task"]["controls"]["can_interrupt"])
        self.assertTrue(interrupted["ok"])
        self.assertEqual(interrupted["task"]["status"], "interrupted")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, "interrupted")
        self.assertEqual(task.resume_policy, "not_resumable")

    async def test_detached_mcp_proxy_task_reconciles_to_lost(self) -> None:
        store = TaskStore(db_path=self.db_path)
        task = store.create_task(
            kind="mcp",
            status="running",
            title="detached mcp",
            runner_payload={"runner": "mcp_proxy", "server": "remote"},
            runner_capabilities={"status": True, "interrupt": True, "cancel": True, "output": True},
            resume_policy="rejoin",
            progress_summary="running",
        )
        controller = TaskController(task_store=store)

        shown = controller.show_task(task.task_id)
        reconciled = controller.reconcile_stale_task(
            task.task_id,
            stale_lost_after_ms=0,
            now_ms=shown["task"]["updated_at_ms"] + 10_000,
        )

        self.assertEqual(shown["task"]["status"], "stale")
        self.assertIsNotNone(reconciled)
        assert reconciled is not None
        self.assertEqual(reconciled.status, "lost")
        self.assertEqual(reconciled.resume_policy, "not_resumable")

    async def test_fast_mcp_job_submit_materializes_external_job_and_polls_status(self) -> None:
        protocol = _job_protocol(output_tool="job_output")
        submit_tool = FakeMcpRuntimeTool(
            name="mcp_remote_start_job",
            result={"job_id": "job-1", "status": "running", "message": "accepted"},
        )
        status_tool = FakeMcpRuntimeTool(name="job_status", result={"status": "completed", "output": "remote done"})
        output_tool = FakeMcpRuntimeTool(name="job_output", result={"output": "full remote output"})
        register_mcp_job_tools("remote", [status_tool, output_tool])

        payload = await run_mcp_tool_with_proxy(
            wrapped_tool=submit_tool,
            server_name="remote",
            transport="http",
            args={"query": "x"},
            tool_context=_tool_context(),
            inline_budget_ms=1000,
            job_protocol=protocol,
        )

        store = TaskStore(db_path=self.db_path)
        task = store.get_task(payload["task_id"])
        self.assertEqual(payload["mode"], "task")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.runner_payload["runner"], "mcp")
        self.assertEqual(task.external_ref, "job-1")
        self.assertEqual(task.status, "running")

        controller = TaskController(task_store=store)
        shown = controller.show_task(task.task_id)
        output = controller.task_output(task.task_id)

        self.assertEqual(shown["task"]["status"], "completed")
        self.assertIn("remote done", shown["task"]["terminal_summary"])
        self.assertIn("full remote output", output["output"])

    async def test_slow_mcp_submit_transitions_proxy_task_to_external_job(self) -> None:
        protocol = _job_protocol()
        submit_tool = FakeMcpRuntimeTool(
            name="mcp_remote_start_job",
            result={"job_id": "job-2", "status": "running"},
            delay_seconds=0.05,
        )
        status_tool = FakeMcpRuntimeTool(name="job_status", result={"status": "running", "message": "still running"})
        register_mcp_job_tools("remote", [status_tool])

        payload = await run_mcp_tool_with_proxy(
            wrapped_tool=submit_tool,
            server_name="remote",
            transport="http",
            args={},
            tool_context=_tool_context(),
            inline_budget_ms=0,
            job_protocol=protocol,
        )

        store = TaskStore(db_path=self.db_path)
        task = store.get_task(payload["task_id"])
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.runner_payload["runner"], "mcp_proxy")

        transitioned = None
        for _ in range(30):
            await asyncio.sleep(0.02)
            transitioned = store.get_task(payload["task_id"])
            if transitioned is not None and transitioned.runner_payload.get("runner") == "mcp":
                break

        self.assertIsNotNone(transitioned)
        assert transitioned is not None
        self.assertEqual(transitioned.runner_payload["runner"], "mcp")
        self.assertEqual(transitioned.external_ref, "job-2")
        self.assertEqual(transitioned.status, "running")

    async def test_mcp_external_job_cancel_uses_configured_cancel_tool(self) -> None:
        protocol = _job_protocol(cancel_tool="job_cancel")
        submit_tool = FakeMcpRuntimeTool(name="mcp_remote_start_job", result={"job_id": "job-3", "status": "running"})
        status_tool = FakeMcpRuntimeTool(name="job_status", result={"status": "running"})
        cancel_tool = FakeMcpRuntimeTool(name="job_cancel", result={"status": "cancelled", "message": "cancelled"})
        register_mcp_job_tools("remote", [status_tool, cancel_tool])

        payload = await run_mcp_tool_with_proxy(
            wrapped_tool=submit_tool,
            server_name="remote",
            transport="http",
            args={},
            tool_context=_tool_context(),
            inline_budget_ms=1000,
            job_protocol=protocol,
        )

        store = TaskStore(db_path=self.db_path)
        controller = TaskController(task_store=store)
        shown = controller.show_task(payload["task_id"])
        cancelled = controller.cancel_task(payload["task_id"])

        self.assertTrue(shown["task"]["controls"]["can_cancel"])
        self.assertTrue(cancelled["ok"])
        self.assertEqual(cancelled["task"]["status"], "cancelled")
        self.assertIn("cancelled", cancelled["task"]["terminal_summary"])


def _job_protocol(*, output_tool: str = "", cancel_tool: str = "") -> McpJobProtocolConfig:
    """Return a test MCP job protocol."""
    return McpJobProtocolConfig(
        enabled=True,
        job_id_path="job_id",
        status_tool="job_status",
        status_args={"job_id": "{job_id}"},
        status_result_path="",
        output_tool=output_tool,
        output_args={"job_id": "{job_id}"},
        output_result_path="",
        cancel_tool=cancel_tool,
        cancel_args={"job_id": "{job_id}"},
        cancel_result_path="",
        poll_timeout_ms=1000,
    )


if __name__ == "__main__":
    unittest.main()
