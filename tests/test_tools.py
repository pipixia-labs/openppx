"""Tests for openppx core tools."""

from __future__ import annotations

import json
from io import BytesIO
import os
import re
import subprocess
import tempfile
import threading
import time
import types as pytypes
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.error import URLError

from openppx.browser.service import BrowserDispatchResponse
from openppx.runtime.checkpoint_schema import TASK_CHECKPOINT_ENVELOPE_SCHEMA, TASK_CHECKPOINT_METADATA_KEY
from openppx.runtime.task_execution import TaskController
from openppx.runtime.task_store import TaskStore
from openppx.runtime.tool_context import route_context
from openppx.tooling.tool_meta import get_tool_meta
from openppx.tooling.registry import (
    SubagentSpawnRequest,
    advance_task_flow,
    browser,
    cancel_task,
    complete_goal,
    computer_task,
    computer_use,
    check_browser_remote_job_protocol,
    configure_browser_runtime,
    configure_heartbeat_waker,
    configure_subagent_dispatcher,
    cron,
    dispatch_task_action,
    message,
    edit_file,
    exec_command,
    finish_task_flow,
    glob,
    grep,
    interrupt_task,
    list_browser_remote_jobs,
    list_browser_remote_providers,
    list_dir,
    list_context_summaries,
    list_skill_api_runners,
    list_task_flows,
    long_task,
    message,
    message_file,
    message_image,
    process_session,
    pause_task,
    read_file,
    resume_task,
    rollup_context_summaries,
    show_task,
    show_task_flow,
    spawn_subagent,
    start_gui_task,
    evaluate_staged_summary_quality_cases,
    summarize_context_text,
    summarize_staged_summary_quality_log,
    task_control_snapshot,
    update_task_flow_step,
    web_fetch,
    web_search,
    write_context_summary,
    write_task_flow,
    write_todos,
    write_file,
)


class ToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        configure_browser_runtime(None)
        configure_heartbeat_waker(None)
        configure_subagent_dispatcher(None)
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_file_tools_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            out = write_file("tmp/demo.txt", "hello world")
            self.assertIn("Successfully wrote", out)
            content = read_file("tmp/demo.txt")
            self.assertEqual(content, "hello world")
            edited = edit_file("tmp/demo.txt", "world", "adk")
            self.assertIn("Successfully edited", edited)
            self.assertEqual(read_file("tmp/demo.txt"), "hello adk")

    def test_file_write_tools_are_blocked_when_filesystem_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            os.environ["OPENPPX_FILESYSTEM_ACCESS"] = "read_only"

            out = write_file("tmp/demo.txt", "hello world")
            self.assertIn("filesystem write is disabled by security policy", out)

            path = Path(tmp) / "tmp" / "demo.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("hello world", encoding="utf-8")
            edited = edit_file("tmp/demo.txt", "world", "adk")
            self.assertIn("filesystem write is disabled by security policy", edited)

    def test_high_risk_tools_are_blocked_without_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            os.environ["OPENPPX_HIGH_RISK_ACTION_ACCESS"] = "false"

            self.assertIn("high-risk action 'message.send' is disabled", message("hello"))
            self.assertIn("high-risk action 'cron.add' is disabled", cron(action="add", message="say hi", every_seconds=60))

    def test_high_risk_tools_require_approval_in_conditional_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            os.environ["OPENPPX_HIGH_RISK_ACTION_ACCESS"] = "conditional"

            self.assertIn("approval required", message("hello"))

    def test_high_risk_tools_allow_confirmed_tool_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            os.environ["OPENPPX_HIGH_RISK_ACTION_ACCESS"] = "conditional"
            tool_context = pytypes.SimpleNamespace(tool_confirmation=pytypes.SimpleNamespace(confirmed=True))

            response = message("hello", channel="local", chat_id="u1", tool_context=tool_context)

            self.assertIn("Message recorded", response)

    def test_builtin_tool_metadata_marks_read_and_high_risk_tools(self) -> None:
        read_meta = get_tool_meta("read_file")
        exec_meta = get_tool_meta("exec")
        runner_catalog_meta = get_tool_meta("list_skill_api_runners")
        pause_meta = get_tool_meta("pause_task")
        snapshot_meta = get_tool_meta("task_control_snapshot")
        dispatch_meta = get_tool_meta("dispatch_task_action")
        summary_eval_meta = get_tool_meta("evaluate_staged_summary_quality_cases")
        summary_log_meta = get_tool_meta("summarize_staged_summary_quality_log")
        browser_contract_meta = get_tool_meta("check_browser_remote_job_protocol")
        remediation_meta = get_tool_meta("remediate_stuck_tasks")
        cleanup_meta = get_tool_meta("cleanup_terminal_tasks")
        orphan_cleanup_meta = get_tool_meta("cleanup_orphan_runtime_facts")
        checkpoint_cleanup_meta = get_tool_meta("cleanup_checkpoint_retention")
        start_gui_meta = get_tool_meta("start_gui_task")

        self.assertIsNotNone(read_meta)
        self.assertIsNotNone(exec_meta)
        self.assertIsNotNone(runner_catalog_meta)
        self.assertIsNotNone(pause_meta)
        self.assertIsNotNone(snapshot_meta)
        self.assertIsNotNone(dispatch_meta)
        self.assertIsNotNone(summary_eval_meta)
        self.assertIsNotNone(summary_log_meta)
        self.assertIsNotNone(browser_contract_meta)
        self.assertIsNotNone(remediation_meta)
        self.assertIsNotNone(cleanup_meta)
        self.assertIsNotNone(orphan_cleanup_meta)
        self.assertIsNotNone(checkpoint_cleanup_meta)
        self.assertIsNotNone(start_gui_meta)
        assert read_meta is not None
        assert exec_meta is not None
        assert runner_catalog_meta is not None
        assert pause_meta is not None
        assert snapshot_meta is not None
        assert dispatch_meta is not None
        assert summary_eval_meta is not None
        assert summary_log_meta is not None
        assert browser_contract_meta is not None
        assert remediation_meta is not None
        assert cleanup_meta is not None
        assert orphan_cleanup_meta is not None
        assert checkpoint_cleanup_meta is not None
        assert start_gui_meta is not None
        self.assertTrue(read_meta.read_only)
        self.assertFalse(exec_meta.read_only)
        self.assertTrue(exec_meta.exclusive)
        self.assertEqual(exec_meta.risk, "high")
        self.assertTrue(runner_catalog_meta.read_only)
        self.assertTrue(summary_eval_meta.read_only)
        self.assertTrue(summary_log_meta.read_only)
        self.assertFalse(pause_meta.read_only)
        self.assertEqual(pause_meta.risk, "medium")
        self.assertTrue(snapshot_meta.read_only)
        self.assertFalse(dispatch_meta.read_only)
        self.assertEqual(dispatch_meta.risk, "high")
        self.assertFalse(browser_contract_meta.read_only)
        self.assertEqual(browser_contract_meta.risk, "high")
        self.assertFalse(remediation_meta.read_only)
        self.assertTrue(remediation_meta.exclusive)
        self.assertEqual(remediation_meta.risk, "high")
        self.assertFalse(cleanup_meta.read_only)
        self.assertTrue(cleanup_meta.exclusive)
        self.assertFalse(checkpoint_cleanup_meta.read_only)
        self.assertTrue(checkpoint_cleanup_meta.exclusive)
        self.assertEqual(checkpoint_cleanup_meta.risk, "high")
        self.assertEqual(cleanup_meta.risk, "high")
        self.assertFalse(orphan_cleanup_meta.read_only)
        self.assertTrue(orphan_cleanup_meta.exclusive)
        self.assertEqual(orphan_cleanup_meta.risk, "high")
        self.assertFalse(start_gui_meta.read_only)
        self.assertTrue(start_gui_meta.exclusive)
        self.assertEqual(start_gui_meta.category, "gui")
        self.assertEqual(start_gui_meta.risk, "high")

    def test_spawn_subagent_respects_delegation_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            os.environ["OPENPPX_CAN_DELEGATE"] = "0"
            ctx = pytypes.SimpleNamespace(
                user_id="user-1",
                session=pytypes.SimpleNamespace(id="session-1"),
                invocation_id="inv-1",
                function_call_id="fc-1",
            )
            out = spawn_subagent(prompt="run task", tool_context=ctx)
            self.assertEqual(out["status"], "error")
            self.assertIn("delegation is disabled", out["error"])

    def test_skill_api_runner_catalog_tool_lists_supported_recipe_runners(self) -> None:
        payload = json.loads(list_skill_api_runners())

        self.assertTrue(payload["ok"])
        names = {item["name"] for item in payload["catalog"]["items"]}
        self.assertEqual(names, {"http", "python", "node", "command"})
        command = next(item for item in payload["catalog"]["items"] if item["name"] == "command")
        self.assertIn(".command.json", command["suffixes"])

    def test_staged_summary_eval_and_log_tools_return_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            os.environ["OPENPPX_WORKSPACE"] = str(workspace)
            cases_dir = workspace / "tests" / "eval"
            cases_dir.mkdir(parents=True)
            cases_path = cases_dir / "staged_summary_quality_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cases": [
                            {
                                "name": "preserve task marker",
                                "source": "Task task_1 wrote artifact_a." + ("A" * 120),
                                "summary": "task_1 wrote artifact_a.",
                                "max_summary_chars": 100,
                                "max_compression_ratio": 0.9,
                                "require_marker_preservation": True,
                                "must_include": ["task_1", "artifact_a"],
                                "expected_ok": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            log_path = workspace / "summary-quality.jsonl"
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps({"outcome": "accepted", "reason": "ok"}),
                        json.dumps({"outcome": "rejected", "reason": "weak_compression"}),
                    ]
                ),
                encoding="utf-8",
            )

            eval_payload = json.loads(evaluate_staged_summary_quality_cases(str(cases_path)))
            log_payload = json.loads(summarize_staged_summary_quality_log(str(log_path)))

        self.assertTrue(eval_payload["ok"])
        self.assertEqual(eval_payload["case_count"], 1)
        self.assertTrue(log_payload["ok"])
        self.assertEqual(log_payload["outcomes"]["accepted"], 1)
        self.assertEqual(log_payload["reasons"]["weak_compression"], 1)

    def test_read_file_supports_file_path_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            write_file("tmp/alias.txt", "alias-ok")
            self.assertEqual(read_file(file_path="tmp/alias.txt"), "alias-ok")

    def test_read_file_supports_offset_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            content = "\n".join(f"line-{idx}" for idx in range(1, 7))
            write_file("tmp/lines.txt", content)

            window = read_file(path="tmp/lines.txt", offset=2, limit=3)
            self.assertIn("line-2\nline-3\nline-4\n", window)
            self.assertIn("[Showing lines 2-4. Use offset=5 to continue.]", window)

            tail = read_file(path="tmp/lines.txt", offset=5)
            self.assertEqual(tail, "line-5\nline-6")

            bad = read_file(path="tmp/lines.txt", offset=0)
            self.assertIn("Error: offset must be a positive integer.", bad)

    def test_read_file_optionally_shows_line_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            write_file("tmp/lines.txt", "alpha\nbeta\ngamma\ndelta\n")

            numbered = read_file(path="tmp/lines.txt", offset=2, limit=2, show_line_numbers=True)
            plain = read_file(path="tmp/lines.txt", offset=2, limit=2, show_line_numbers=False)

            self.assertIn("2| beta\n3| gamma\n", numbered)
            self.assertIn("[Showing lines 2-3. Use offset=4 to continue.]", numbered)
            self.assertIn("beta\ngamma\n", plain)
            self.assertNotIn("2| beta", plain)

    def test_read_file_limit_appends_continuation_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            content = "\n".join(f"line-{idx}" for idx in range(1, 7))
            write_file("tmp/lines.txt", content)

            page = read_file(path="tmp/lines.txt", offset=2, limit=2)
            self.assertIn("line-2", page)
            self.assertIn("line-3", page)
            self.assertIn("[Showing lines 2-3. Use offset=4 to continue.]", page)

    def test_read_file_caps_output_without_explicit_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            os.environ["OPENPPX_READ_FILE_MAX_BYTES"] = "1024"
            big = "\n".join(f"line-{idx}-{'x' * 80}" for idx in range(1, 400))
            write_file("tmp/big.txt", big)

            output = read_file(path="tmp/big.txt")
            self.assertIn("line-1-", output)
            self.assertIn("[Read output capped at 1KB for this call. Use offset=", output)
            self.assertNotIn("line-399", output)

    def test_read_file_rejects_device_paths(self) -> None:
        out = read_file(path="/dev/random")
        self.assertIn("Refusing to read device", out)

    def test_read_file_returns_image_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            image_path = Path(tmp) / "tmp" / "demo.png"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

            payload = json.loads(read_file(path="tmp/demo.png"))

            self.assertEqual(payload["type"], "image")
            self.assertEqual(payload["mimeType"], "image/png")
            self.assertEqual(Path(payload["path"]).resolve(), image_path.resolve())

    def test_read_file_extracts_docx_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            docx_path = Path(tmp) / "tmp" / "demo.docx"
            docx_path.parent.mkdir(parents=True, exist_ok=True)
            document_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p></w:body></w:document>"
            )
            with zipfile.ZipFile(docx_path, "w") as archive:
                archive.writestr("word/document.xml", document_xml)

            output = read_file(path="tmp/demo.docx")

            self.assertIn("Hello DOCX", output)

    def test_list_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            Path(tmp, "a").mkdir()
            Path(tmp, "b.txt").write_text("x", encoding="utf-8")
            listing = list_dir(".")
            self.assertIn("[D] a", listing)
            self.assertIn("[F] b.txt", listing)

    def test_list_dir_supports_recursive_and_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            Path(tmp, "a").mkdir()
            Path(tmp, "a", "nested.txt").write_text("x", encoding="utf-8")
            Path(tmp, "b.txt").write_text("x", encoding="utf-8")

            listing = list_dir(".", recursive=True, max_entries=2)
            self.assertIn("a/", listing)
            self.assertIn("a/nested.txt", listing)
            self.assertIn("truncated", listing)

    def test_glob_finds_matching_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            Path(tmp, "src").mkdir()
            Path(tmp, "src", "main.py").write_text("print('x')", encoding="utf-8")
            Path(tmp, "src", "util.txt").write_text("demo", encoding="utf-8")

            output = glob("*.py", path="src")
            self.assertIn("src/main.py", output)
            self.assertNotIn("util.txt", output)

    def test_grep_supports_content_mode_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            Path(tmp, "src").mkdir()
            Path(tmp, "src", "main.py").write_text("one\ntwo target\nthree\n", encoding="utf-8")

            output = grep(
                "target",
                path="src",
                output_mode="content",
                context_before=1,
                context_after=1,
            )
            self.assertIn("src/main.py:2", output)
            self.assertIn("  1| one", output)
            self.assertIn("> 2| two target", output)
            self.assertIn("  3| three", output)

    def test_edit_file_supports_trimmed_line_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            write_file("tmp/demo.txt", "alpha\n  beta  \ngamma\n")

            edited = edit_file("tmp/demo.txt", "beta", "delta")
            self.assertIn("Successfully edited", edited)
            self.assertIn("delta", read_file("tmp/demo.txt"))

    def test_edit_file_supports_quote_normalized_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            file_path = Path(tmp) / "tmp" / "quotes.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("label = \u201cold\u201d\n", encoding="utf-8")

            edited = edit_file("tmp/quotes.txt", 'label = "old"', 'label = "new"')

            self.assertIn("Successfully edited", edited)
            self.assertIn("label = \u201cnew\u201d", file_path.read_text(encoding="utf-8"))

    def test_exec_tool(self) -> None:
        result = exec_command("echo hello")
        self.assertIn("hello", result)

    def test_exec_tool_wraps_command_with_sandbox(self) -> None:
        captured: dict[str, object] = {}

        def _fake_run(*args, **kwargs):
            captured["argv"] = args[0]
            return pytypes.SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

        with patch("openppx.tooling.registry.subprocess.run", side_effect=_fake_run):
            out = exec_command("echo hello", sandbox="bwrap")

        argv = captured.get("argv")
        self.assertIsInstance(argv, list)
        self.assertIn("bwrap", " ".join(str(part) for part in argv))
        self.assertIn("ok", out)

    def test_exec_tool_configured_docker_backend_does_not_auto_sandbox(self) -> None:
        captured: dict[str, object] = {}

        def _fake_run(*args, **kwargs):
            captured["argv"] = args[0]
            return pytypes.SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

        os.environ["OPENPPX_SANDBOX_BACKEND"] = "docker"
        with patch("openppx.tooling.registry.subprocess.run", side_effect=_fake_run):
            out = exec_command("echo hello")

        self.assertIn("ok", out)
        self.assertEqual(captured["argv"], ["echo", "hello"])

    def test_exec_tool_blocks_backend_downgrade_to_bwrap(self) -> None:
        os.environ["OPENPPX_SANDBOX_BACKEND"] = "docker"
        with patch("openppx.tooling.registry.subprocess.run") as mocked_run:
            out = exec_command("echo hello", sandbox="bwrap")

        self.assertIn("downgrade", out.lower())
        mocked_run.assert_not_called()

    def test_exec_tool_builds_explicit_docker_sandbox_command(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def _fake_run(args, **kwargs):
            calls.append((list(args), kwargs))
            return pytypes.SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            resolved_workspace = workspace.resolve(strict=False)
            (workspace / ".git").mkdir()
            (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
            (workspace / ".ssh").mkdir()
            os.environ["OPENPPX_WORKSPACE"] = tmp
            with patch("openppx.tooling.registry.subprocess.run", side_effect=_fake_run):
                out = exec_command("echo hello", sandbox="docker")

        self.assertIn("ok", out)
        argv = calls[0][0]
        self.assertEqual(argv[:2], ["docker", "run"])
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertIn("openppx-sandbox:dev", argv)
        self.assertEqual(argv[-2:], ["echo", "hello"])
        mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "--mount"]
        self.assertIn(f"type=bind,src={resolved_workspace},dst={resolved_workspace}", mounts)
        self.assertIn(
            f"type=bind,src={resolved_workspace / '.git'},dst={resolved_workspace / '.git'},readonly",
            mounts,
        )
        self.assertIn(f"type=bind,src=/dev/null,dst={resolved_workspace / '.env'},readonly", mounts)
        self.assertIn(
            f"type=tmpfs,dst={resolved_workspace / '.ssh'},tmpfs-mode=0700,tmpfs-size=1m",
            mounts,
        )

    def test_exec_tool_docker_sandbox_uses_container_shell(self) -> None:
        calls: list[list[str]] = []

        def _fake_run(args, **kwargs):
            calls.append(list(args))
            return pytypes.SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            with patch("openppx.tooling.registry.subprocess.run", side_effect=_fake_run):
                out = exec_command("export OPENPPX_EXEC_TEST=hello && echo $OPENPPX_EXEC_TEST", sandbox="docker")

        self.assertIn("ok", out)
        argv = calls[0]
        image_index = argv.index("openppx-sandbox:dev")
        self.assertEqual(argv[image_index + 1 : image_index + 3], ["/bin/sh", "-lc"])

    def test_exec_tool_docker_sandbox_rejects_session_modes(self) -> None:
        out = exec_command("echo hello", sandbox="docker", background=True)
        self.assertIn("foreground exec only", out.lower())

    def test_exec_tool_docker_sandbox_timeout_cleans_container(self) -> None:
        calls: list[list[str]] = []

        def _fake_run(args, **kwargs):
            argv = list(args)
            calls.append(argv)
            if argv[:2] == ["docker", "run"]:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return pytypes.SimpleNamespace(stdout="", stderr="", returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            with patch("openppx.tooling.registry.subprocess.run", side_effect=_fake_run):
                out = exec_command("echo hello", sandbox="docker")

        container_name = calls[0][calls[0].index("--name") + 1]
        self.assertIn("timed out", out.lower())
        self.assertIn("cleanup requested", out.lower())
        self.assertEqual(calls[1], ["docker", "kill", container_name])
        self.assertEqual(calls[2], ["docker", "rm", "-f", container_name])

    def test_computer_use_tool_calls_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            payload = {"ok": True, "arguments": {"action": "wait", "time": 1}}
            with patch("openppx.tooling.registry.execute_gui_action", return_value=payload) as mocked:
                result = computer_use("wait 1 second", dry_run=True, model="m", api_key="k")
        self.assertIn('"ok": true', result)
        mocked.assert_called_once()

    def test_computer_task_tool_calls_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            payload = {"ok": True, "finished": True, "message": "done", "steps": []}
            with patch("openppx.tooling.registry.execute_gui_task", return_value=payload) as mocked:
                result = computer_task("finish login flow", max_steps=5, dry_run=True, planner_model="m", planner_api_key="k")
        self.assertIn('"ok": true', result)
        mocked.assert_called_once()

    def test_task_control_snapshot_and_dispatch_tools_return_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            task = TaskStore().create_task(
                kind="manual",
                status="completed",
                title="done",
                terminal_summary="finished output",
                runner_capabilities={"output": True},
            )

            snapshot = json.loads(task_control_snapshot(task_id=task.task_id))
            output = json.loads(dispatch_task_action(task.task_id, "inspect_output"))

            self.assertTrue(snapshot["ok"])
            self.assertEqual(snapshot["items"][0]["task_id"], task.task_id)
            self.assertTrue(output["ok"])
            self.assertIn("finished output", output["output"])

    def test_computer_task_materializes_long_running_builtin_gui_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")

            def _slow_task(**_kwargs):
                time.sleep(0.05)
                return {"ok": True, "finished": True, "message": "done", "steps": []}

            with patch("openppx.tooling.registry.execute_gui_task", side_effect=_slow_task):
                result = json.loads(
                    computer_task(
                        "finish login flow",
                        max_steps=5,
                        dry_run=True,
                        planner_model="m",
                        planner_api_key="k",
                        inline_budget_ms=0,
                    )
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "task")
            self.assertEqual(result["status"], "running")
            task_id = result["task_id"]
            store = TaskStore()
            shown = TaskController(task_store=store).show_task(task_id)
            self.assertEqual(shown["task"]["status"], "running")
            self.assertTrue(shown["task"]["controls"]["can_interrupt"])
            self.assertTrue(shown["task"]["controls"]["can_cancel"])
            self.assertTrue(shown["task"]["controls"]["can_resume"])

            completed = None
            for _ in range(30):
                time.sleep(0.02)
                completed = store.get_task(task_id)
                if completed is not None and completed.status == "completed":
                    break

            self.assertIsNotNone(completed)
            assert completed is not None
            self.assertEqual(completed.status, "completed")
            self.assertIn("done", completed.terminal_summary)

    def test_computer_task_cooperative_interrupt_stops_background_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            started = threading.Event()

            def _cancellable_task(**kwargs):
                cancel_token = kwargs["cancel_token"]
                started.set()
                while True:
                    cancel_token.check_cancelled()
                    time.sleep(0.01)

            with patch("openppx.tooling.registry.execute_gui_task", side_effect=_cancellable_task):
                result = json.loads(
                    computer_task(
                        "finish login flow",
                        max_steps=5,
                        dry_run=True,
                        planner_model="m",
                        planner_api_key="k",
                        inline_budget_ms=0,
                    )
                )
                self.assertTrue(started.wait(timeout=1.0))
                interrupted = json.loads(interrupt_task(result["task_id"]))

            self.assertTrue(interrupted["ok"])
            self.assertEqual(interrupted["action"], "stop_requested")
            store = TaskStore()
            stopped = None
            for _ in range(50):
                time.sleep(0.02)
                stopped = store.get_task(result["task_id"])
                if stopped is not None and stopped.status == "interrupted":
                    break

            self.assertIsNotNone(stopped)
            assert stopped is not None
            self.assertEqual(stopped.status, "interrupted")
            events = [
                event.event_type
                for event in TaskController(task_store=store).event_store.list_events(stopped.task_id)
            ]
            self.assertIn("task.interrupt_requested", events)
            self.assertIn("task.interrupted", events)

    def test_computer_task_cooperative_cancel_marks_background_task_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            started = threading.Event()

            def _cancellable_task(**kwargs):
                cancel_token = kwargs["cancel_token"]
                started.set()
                while True:
                    cancel_token.check_cancelled()
                    time.sleep(0.01)

            with patch("openppx.tooling.registry.execute_gui_task", side_effect=_cancellable_task):
                result = json.loads(
                    computer_task(
                        "finish login flow",
                        max_steps=5,
                        dry_run=True,
                        planner_model="m",
                        planner_api_key="k",
                        inline_budget_ms=0,
                    )
                )
                self.assertTrue(started.wait(timeout=1.0))
                cancelled = json.loads(cancel_task(result["task_id"]))

            self.assertTrue(cancelled["ok"])
            self.assertEqual(cancelled["action"], "stop_requested")
            store = TaskStore()
            stopped = None
            for _ in range(50):
                time.sleep(0.02)
                stopped = store.get_task(result["task_id"])
                if stopped is not None and stopped.status == "cancelled":
                    break

            self.assertIsNotNone(stopped)
            assert stopped is not None
            self.assertEqual(stopped.status, "cancelled")
            self.assertIn("cancellation requested", stopped.terminal_summary)
            events = [
                event.event_type
                for event in TaskController(task_store=store).event_store.list_events(stopped.task_id)
            ]
            self.assertIn("task.cancel_requested", events)
            self.assertIn("task.cancelled", events)

    def test_computer_task_background_failure_marks_task_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")

            def _slow_failed_task(**_kwargs):
                time.sleep(0.05)
                return {"ok": False, "error": "gui failed", "steps": []}

            with patch("openppx.tooling.registry.execute_gui_task", side_effect=_slow_failed_task):
                result = json.loads(
                    computer_task(
                        "finish login flow",
                        max_steps=5,
                        dry_run=True,
                        planner_model="m",
                        planner_api_key="k",
                        inline_budget_ms=0,
                    )
                )

            store = TaskStore()
            failed = None
            controller = TaskController(task_store=store)
            for _ in range(30):
                time.sleep(0.02)
                failed = store.get_task(result["task_id"])
                events = controller.event_store.list_events(result["task_id"]) if failed is not None else []
                event_types = {event.event_type for event in events}
                if failed is not None and failed.status == "failed" and "task.failed" in event_types:
                    break

            self.assertIsNotNone(failed)
            assert failed is not None
            self.assertEqual(failed.status, "failed")
            self.assertIn("gui failed", failed.last_error)

    def test_start_gui_task_materializes_checkpointable_gui_job_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            ctx = pytypes.SimpleNamespace(
                user_id="user-1",
                session=pytypes.SimpleNamespace(id="session-1"),
                invocation_id="inv-1",
                function_call_id="fc-1",
            )
            job_payload = {
                "ok": True,
                "job_id": "gui_job_1",
                "status": "running",
                "checkpoint": {
                    "task": "finish login flow",
                    "current_plan": "finish login flow",
                    "history": [],
                    "next_step": 1,
                    "summary": "GUI job checkpoint before step 1.",
                },
            }
            with patch("openppx.tooling.registry.submit_gui_task_job", return_value=job_payload) as submit:
                first = json.loads(
                    start_gui_task(
                        "finish login flow",
                        max_steps=5,
                        dry_run=True,
                        planner_model="m",
                        planner_api_key="k",
                        tool_context=ctx,
                    )
                )
                second = json.loads(
                    start_gui_task(
                        "finish login flow",
                        max_steps=5,
                        dry_run=True,
                        planner_model="m",
                        planner_api_key="k",
                        tool_context=ctx,
                    )
                )

            self.assertTrue(first["ok"])
            self.assertEqual(first["mode"], "task")
            self.assertEqual(first["status"], "running")
            self.assertEqual(first["job_id"], "gui_job_1")
            self.assertEqual(second["task_id"], first["task_id"])
            self.assertTrue(second["replayed"])
            submit.assert_called_once()
            with patch(
                "openppx.runtime.task_execution.gui_task_job_status",
                return_value={
                    "ok": True,
                    "job_id": "gui_job_1",
                    "status": "running",
                    "summary": "GUI job `gui_job_1` started.",
                    "checkpoint": job_payload["checkpoint"],
                    "result": {},
                },
            ):
                shown = TaskController(task_store=TaskStore()).show_task(first["task_id"])
            self.assertEqual(shown["task"]["external_ref"], "gui_job_1")
            self.assertEqual(shown["task"]["controls"]["pause_tool"], "pause_task")
            self.assertEqual(shown["checkpoints"][0]["payload"]["next_step"], 1)

    def test_start_gui_task_show_pause_resume_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            ctx = pytypes.SimpleNamespace(
                user_id="user-1",
                session=pytypes.SimpleNamespace(id="session-1"),
                invocation_id="inv-1",
                function_call_id="fc-1",
            )
            initial_checkpoint = {
                "task": "finish login flow",
                "current_plan": "finish login flow",
                "history": [],
                "next_step": 1,
                "summary": "GUI job checkpoint before step 1.",
            }
            paused_checkpoint = {
                "task": "finish login flow",
                "current_plan": "continue after login click",
                "history": [{"step": 1, "type": "execute", "action": "click login"}],
                "next_step": 2,
                "summary": "Paused after step 1.",
            }
            with patch(
                "openppx.tooling.registry.submit_gui_task_job",
                return_value={"ok": True, "job_id": "gui_job_old", "status": "running", "checkpoint": initial_checkpoint},
            ):
                started = json.loads(start_gui_task("finish login flow", max_steps=5, tool_context=ctx))

            task_id = started["task_id"]
            running_status = {
                "ok": True,
                "job_id": "gui_job_old",
                "status": "running",
                "summary": "Running step 1.",
                "checkpoint": initial_checkpoint,
                "result": {},
            }
            paused_status = {
                "ok": True,
                "job_id": "gui_job_old",
                "status": "paused",
                "summary": "Paused after step 1.",
                "checkpoint": paused_checkpoint,
                "result": {},
            }
            with patch("openppx.runtime.task_execution.gui_task_job_status", return_value=running_status):
                shown_running = json.loads(show_task(task_id))

            with patch("openppx.runtime.task_execution.gui_task_job_status", return_value=running_status):
                with patch(
                    "openppx.runtime.task_execution.gui_task_job_cancel",
                    return_value={"ok": True, "job_id": "gui_job_old", "status": "stop_requested", "action": "paused"},
                ) as cancel:
                    paused_request = json.loads(pause_task(task_id))

            with patch("openppx.runtime.task_execution.gui_task_job_status", return_value=paused_status):
                shown_paused = json.loads(show_task(task_id))

            with patch("openppx.runtime.task_execution.gui_task_job_status", return_value=paused_status):
                with patch(
                    "openppx.runtime.task_execution.resume_gui_task_job",
                    return_value={"ok": True, "job_id": "gui_job_new", "status": "running"},
                ) as resume:
                    resumed = json.loads(resume_task(task_id))

            self.assertTrue(started["ok"])
            self.assertEqual(shown_running["task"]["status"], "running")
            self.assertEqual(shown_running["task"]["controls"]["pause_tool"], "pause_task")
            self.assertEqual(paused_request["action"], "pause_requested")
            self.assertFalse(paused_request["task"]["controls"]["can_pause"])
            self.assertEqual(shown_paused["task"]["status"], "paused")
            self.assertEqual(shown_paused["task"]["controls"]["resume_tool"], "resume_task")
            shown_checkpoint = shown_paused["checkpoints"][0]["payload"]
            self.assertEqual(shown_checkpoint["task"], paused_checkpoint["task"])
            self.assertEqual(shown_checkpoint["history"], paused_checkpoint["history"])
            self.assertEqual(shown_checkpoint["next_step"], paused_checkpoint["next_step"])
            self.assertEqual(
                shown_checkpoint[TASK_CHECKPOINT_METADATA_KEY]["schema"],
                TASK_CHECKPOINT_ENVELOPE_SCHEMA,
            )
            self.assertTrue(resumed["ok"])
            self.assertEqual(resumed["action"], "resumed")
            self.assertEqual(resumed["task"]["status"], "running")
            self.assertEqual(resumed["task"]["external_ref"], "gui_job_new")
            cancel.assert_called_once_with(
                "gui_job_old",
                terminal_status="paused",
                reason="GUI job pause requested by user.",
            )
            resume_checkpoint = resume.call_args.kwargs["checkpoint"]
            self.assertEqual(resume_checkpoint["task"], paused_checkpoint["task"])
            self.assertEqual(resume_checkpoint["history"], paused_checkpoint["history"])
            self.assertEqual(resume_checkpoint["next_step"], paused_checkpoint["next_step"])
            self.assertEqual(
                resume_checkpoint[TASK_CHECKPOINT_METADATA_KEY]["schema"],
                TASK_CHECKPOINT_ENVELOPE_SCHEMA,
            )

    def test_goal_mirror_tools_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            ctx = pytypes.SimpleNamespace(session=pytypes.SimpleNamespace(id="session-1"))

            goal_payload = json.loads(
                long_task(
                    "Finish long-task implementation",
                    completion_criteria="Tests pass",
                    current_summary="Context phase",
                    tool_context=ctx,
                )
            )
            todo_payload = json.loads(
                write_todos(
                    [
                        {"content": "Add store", "status": "completed"},
                        {"content": "Add tools", "status": "pending"},
                    ],
                    tool_context=ctx,
                )
            )
            completed_payload = json.loads(complete_goal(final_summary="Done", tool_context=ctx))

            self.assertTrue(goal_payload["ok"])
            self.assertEqual(goal_payload["goal"]["session_id"], "session-1")
            self.assertTrue(todo_payload["ok"])
            self.assertEqual([item["status"] for item in todo_payload["items"]], ["completed", "in_progress"])
            self.assertTrue(completed_payload["ok"])
            self.assertEqual(completed_payload["goal"]["status"], "completed")
            self.assertEqual(completed_payload["goal"]["current_summary"], "Done")

    def test_task_flow_tools_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            ctx = pytypes.SimpleNamespace(session=pytypes.SimpleNamespace(id="session-1"))

            written = json.loads(
                write_task_flow(
                    "Ship TaskFlow",
                    [
                        {"title": "Add store", "status": "in_progress"},
                        {"title": "Add tools", "status": "pending"},
                    ],
                    tool_context=ctx,
                )
            )
            flow_id = written["flow"]["flow_id"]
            first_step_id = written["steps"][0]["step_id"]
            updated = json.loads(
                update_task_flow_step(
                    flow_id,
                    step_id=first_step_id,
                    status="completed",
                    evidence={"tests": "ok"},
                )
            )
            shown = json.loads(show_task_flow(flow_id=flow_id, tool_context=ctx))
            listed = json.loads(list_task_flows(tool_context=ctx))
            finished = json.loads(finish_task_flow(flow_id=flow_id, evidence={"done": True}, tool_context=ctx))

            self.assertTrue(written["ok"])
            self.assertEqual([step["status"] for step in written["steps"]], ["in_progress", "pending"])
            self.assertTrue(updated["ok"])
            self.assertEqual(updated["step"]["status"], "completed")
            self.assertEqual(updated["step"]["evidence"]["tests"], "ok")
            self.assertTrue(shown["ok"])
            self.assertEqual([step["status"] for step in shown["steps"]], ["completed", "in_progress"])
            self.assertEqual(listed["items"][0]["flow_id"], flow_id)
            self.assertTrue(finished["ok"])
            self.assertEqual(finished["flow"]["status"], "completed")

    def test_advance_task_flow_syncs_bound_task_and_promotes_ready_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            ctx = pytypes.SimpleNamespace(session=pytypes.SimpleNamespace(id="session-1"))
            task = TaskStore().create_task(
                kind="skill_api",
                status="completed",
                title="Download data",
                session_id="session-1",
                terminal_summary="download complete",
            )
            written = json.loads(
                write_task_flow(
                    "Run workflow",
                    [
                        {"step_id": "download", "title": "Download", "status": "in_progress", "task_id": task.task_id},
                        {"step_id": "analyze", "title": "Analyze", "depends_on": ["download"]},
                    ],
                    tool_context=ctx,
                )
            )

            advanced = json.loads(advance_task_flow(flow_id=written["flow"]["flow_id"], tool_context=ctx))

            self.assertTrue(advanced["ok"])
            self.assertEqual([step["status"] for step in advanced["steps"]], ["completed", "in_progress"])
            self.assertEqual(advanced["synced_tasks"][0]["task_status"], "completed")
            self.assertEqual(advanced["steps"][0]["evidence"]["task_summary"], "download complete")
            self.assertEqual(advanced["projection"]["active_step_ids"], ["analyze"])

    def test_context_summary_tools_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            ctx = pytypes.SimpleNamespace(session=pytypes.SimpleNamespace(id="session-1"))

            written = json.loads(
                write_context_summary(
                    "Keep summary separate from TaskRun facts.",
                    title="Summary rule",
                    flow_id="flow-1",
                    scope="flow",
                    tool_context=ctx,
                )
            )
            summarized = json.loads(
                summarize_context_text(
                    "A" * 120 + "\n" + "B" * 120,
                    title="Large text",
                    max_chars=90,
                    tool_context=ctx,
                )
            )
            listed = json.loads(list_context_summaries(flow_id="flow-1", tool_context=ctx))
            rollup = json.loads(
                rollup_context_summaries(
                    target_scope="flow",
                    source_scope="flow",
                    flow_id="flow-1",
                    title="Flow summary",
                    tool_context=ctx,
                )
            )

            self.assertTrue(written["ok"])
            self.assertEqual(written["summary"]["scope"], "flow")
            self.assertEqual(written["summary"]["flow_id"], "flow-1")
            self.assertTrue(summarized["ok"])
            self.assertIn("context summary truncated", summarized["summary"]["content"])
            self.assertEqual(listed["items"][0]["summary_id"], written["summary"]["summary_id"])
            self.assertTrue(rollup["ok"])
            self.assertEqual(rollup["summary"]["scope"], "flow")
            self.assertEqual(rollup["summary"]["source_kind"], "summary_rollup")
            self.assertIn("Keep summary separate", rollup["summary"]["content"])

    def test_exec_tool_requests_heartbeat_wake(self) -> None:
        reasons: list[str] = []
        configure_heartbeat_waker(reasons.append)

        result = exec_command("echo hello")
        self.assertIn("hello", result)
        self.assertIn("exec:foreground", reasons)

    def test_process_actions_request_heartbeat_wake(self) -> None:
        class _DummyManager:
            def write_session(self, session_id, data, *, eof, scope_key):
                return None

            def kill_session(self, session_id, *, scope_key):
                return None

        reasons: list[str] = []
        configure_heartbeat_waker(reasons.append)
        with patch("openppx.tooling.registry.get_process_session_manager", return_value=_DummyManager()):
            process_session("write", session_id="s1", data="abc")
            process_session("send-keys", session_id="s1", literal="x")
            process_session("submit", session_id="s1")
            process_session("paste", session_id="s1", data="paste")
            process_session("kill", session_id="s1")

        self.assertIn("exec:write", reasons)
        self.assertIn("exec:send-keys", reasons)
        self.assertIn("exec:submit", reasons)
        self.assertIn("exec:paste", reasons)
        self.assertIn("exec:kill", reasons)

    def test_exec_background_then_poll_and_remove(self) -> None:
        cmd = (
            'python -c "import time,sys;print(\'start\');sys.stdout.flush();'
            "time.sleep(0.4);print('end')\""
        )
        out = exec_command(cmd, yield_ms=20)
        self.assertIn("session", out.lower())
        matched = re.search(r"session ([0-9a-f-]+)", out)
        self.assertIsNotNone(matched)
        session_id = matched.group(1) if matched else ""

        deadline = time.time() + 3
        last_poll = ""
        while time.time() < deadline:
            last_poll = process_session("poll", session_id=session_id, timeout_ms=200)
            if "Process exited with code" in last_poll:
                break

        self.assertIn("Process exited with code", last_poll)
        log_text = process_session("log", session_id=session_id)
        self.assertIn("start", log_text)
        self.assertIn("end", log_text)
        removed = process_session("remove", session_id=session_id)
        self.assertIn("Removed session", removed)

    def test_exec_background_records_feedback_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            out = exec_command('python -c "import time; time.sleep(0.2)"', background=True)
            self.assertIn("session", out.lower())

            outbox = Path(tmp) / "messages" / "outbox.log"
            self.assertTrue(outbox.exists())
            records = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines()]
            statuses = {
                str(record.get("metadata", {}).get("_feedback_status", ""))
                for record in records
                if isinstance(record.get("metadata"), dict)
            }
            step_phases = {
                str(record.get("metadata", {}).get("_step_phase", ""))
                for record in records
                if isinstance(record.get("metadata"), dict)
            }
            tool_names = {
                str(record.get("metadata", {}).get("_tool_name", ""))
                for record in records
                if isinstance(record.get("metadata"), dict)
            }
            self.assertIn("started", statuses)
            self.assertIn("running", statuses)
            self.assertIn("started", step_phases)
            self.assertIn("running", step_phases)
            self.assertIn("exec", tool_names)

    def test_exec_background_write_stdin(self) -> None:
        cmd = 'python -c "import sys;print(sys.stdin.readline().strip())"'
        out = exec_command(cmd, background=True)
        matched = re.search(r"session ([0-9a-f-]+)", out)
        self.assertIsNotNone(matched)
        session_id = matched.group(1) if matched else ""

        write_out = process_session("write", session_id=session_id, data="hello\\n", eof=True)
        self.assertIn("Wrote", write_out)

        deadline = time.time() + 3
        last_poll = ""
        while time.time() < deadline:
            last_poll = process_session("poll", session_id=session_id, timeout_ms=200)
            if "Process exited with code" in last_poll:
                break
        self.assertIn("Process exited with code", last_poll)
        log_text = process_session("log", session_id=session_id)
        self.assertIn("hello", log_text.lower())

    def test_process_poll_records_feedback_output_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            cmd = (
                'python -c "import sys,time;print(\'hello\');sys.stdout.flush();'
                'time.sleep(0.3)"'
            )
            out = exec_command(cmd, background=True)
            matched = re.search(r"session ([0-9a-f-]+)", out)
            self.assertIsNotNone(matched)
            session_id = matched.group(1) if matched else ""

            poll = process_session("poll", session_id=session_id, timeout_ms=400)
            self.assertIn("[poll-meta]", poll)

            outbox = Path(tmp) / "messages" / "outbox.log"
            records = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines()]
            output_events = [
                record for record in records
                if str(record.get("metadata", {}).get("_feedback_type", "")) == "tool_output"
            ]
            self.assertTrue(output_events)
            self.assertEqual(output_events[-1]["metadata"]["_tool_name"], "process")
            self.assertEqual(output_events[-1]["metadata"]["_session_id"], session_id)
            self.assertEqual(output_events[-1]["metadata"]["_event_class"], "step_output")
            self.assertEqual(output_events[-1]["metadata"]["_step_id"], session_id)

    def test_exec_background_send_keys(self) -> None:
        cmd = 'python -c "import sys;print(sys.stdin.readline().strip())"'
        out = exec_command(cmd, background=True)
        matched = re.search(r"session ([0-9a-f-]+)", out)
        self.assertIsNotNone(matched)
        session_id = matched.group(1) if matched else ""

        send_out = process_session(
            "send-keys",
            session_id=session_id,
            literal="hello",
            keys=["Enter"],
            eof=True,
        )
        self.assertIn("Sent", send_out)

        deadline = time.time() + 3
        last_poll = ""
        while time.time() < deadline:
            last_poll = process_session("poll", session_id=session_id, timeout_ms=200)
            if "Process exited with code" in last_poll:
                break
        self.assertIn("Process exited with code", last_poll)
        log_text = process_session("log", session_id=session_id)
        self.assertIn("hello", log_text.lower())

    def test_process_log_supports_offset_and_limit(self) -> None:
        cmd = 'python -c "import sys;[print(f\'line-{i}\') for i in range(6)]"'
        out = exec_command(cmd, background=True)
        matched = re.search(r"session ([0-9a-f-]+)", out)
        self.assertIsNotNone(matched)
        session_id = matched.group(1) if matched else ""

        deadline = time.time() + 3
        last_poll = ""
        while time.time() < deadline:
            last_poll = process_session("poll", session_id=session_id, timeout_ms=200)
            if "Process exited with code" in last_poll:
                break
        self.assertIn("Process exited with code", last_poll)

        page = process_session("log", session_id=session_id, offset=2, limit=2)
        meta_line = page.splitlines()[0] if page else ""
        self.assertTrue(meta_line.startswith("[log-meta]"))
        meta = json.loads(meta_line[len("[log-meta]") :])
        self.assertEqual(meta["total_lines"], 6)
        self.assertEqual(meta["offset"], 2)
        self.assertEqual(meta["returned_lines"], 2)
        self.assertEqual(meta["window_limit"], 2)
        self.assertIn("truncated", meta)
        self.assertIn("line-2", page)
        self.assertIn("line-3", page)
        self.assertNotIn("line-4", page)

        removed = process_session("remove", session_id=session_id)
        self.assertIn("Removed session", removed)

    def test_exec_background_send_keys_supports_hex_values(self) -> None:
        cmd = 'python -c "import sys;print(sys.stdin.readline().strip())"'
        out = exec_command(cmd, background=True)
        matched = re.search(r"session ([0-9a-f-]+)", out)
        self.assertIsNotNone(matched)
        session_id = matched.group(1) if matched else ""

        send_out = process_session(
            "send-keys",
            session_id=session_id,
            literal="hello",
            hex_values=["0d"],
            eof=True,
        )
        self.assertIn("Sent", send_out)

        deadline = time.time() + 3
        last_poll = ""
        while time.time() < deadline:
            last_poll = process_session("poll", session_id=session_id, timeout_ms=200)
            if "Process exited with code" in last_poll:
                break
        self.assertIn("Process exited with code", last_poll)
        log_text = process_session("log", session_id=session_id)
        self.assertIn("hello", log_text.lower())

    def test_exec_background_paste_bracketed_and_plain(self) -> None:
        cmd = 'python -c "import sys;print(sys.stdin.buffer.read().hex())"'

        out1 = exec_command(cmd, background=True)
        matched1 = re.search(r"session ([0-9a-f-]+)", out1)
        self.assertIsNotNone(matched1)
        session1 = matched1.group(1) if matched1 else ""
        paste1 = process_session("paste", session_id=session1, data="abc")
        self.assertIn("bracketed", paste1.lower())
        process_session("write", session_id=session1, eof=True)
        deadline = time.time() + 3
        poll1 = ""
        while time.time() < deadline:
            poll1 = process_session("poll", session_id=session1, timeout_ms=200)
            if "Process exited with code" in poll1:
                break
        self.assertIn("Process exited with code", poll1)
        log1 = process_session("log", session_id=session1)
        self.assertIn("1b5b3230307e6162631b5b3230317e", log1.lower())

        out2 = exec_command(cmd, background=True)
        matched2 = re.search(r"session ([0-9a-f-]+)", out2)
        self.assertIsNotNone(matched2)
        session2 = matched2.group(1) if matched2 else ""
        paste2 = process_session("paste", session_id=session2, data="abc", bracketed=False)
        self.assertIn("plain", paste2.lower())
        process_session("write", session_id=session2, eof=True)
        poll2 = ""
        deadline = time.time() + 3
        while time.time() < deadline:
            poll2 = process_session("poll", session_id=session2, timeout_ms=200)
            if "Process exited with code" in poll2:
                break
        self.assertIn("Process exited with code", poll2)
        log2 = process_session("log", session_id=session2)
        self.assertIn("616263", log2.lower())
        self.assertNotIn("1b5b3230307e", log2.lower())

    def test_process_poll_returns_retry_hint(self) -> None:
        cmd = 'python -c "import time;time.sleep(0.8);print(\'done\')"'
        out = exec_command(cmd, background=True)
        matched = re.search(r"session ([0-9a-f-]+)", out)
        self.assertIsNotNone(matched)
        session_id = matched.group(1) if matched else ""

        poll = process_session("poll", session_id=session_id, timeout_ms=10)
        first_line = poll.splitlines()[0] if poll else ""
        self.assertTrue(first_line.startswith("[poll-meta]"))
        meta = json.loads(first_line[len("[poll-meta]") :])
        self.assertEqual(meta["status"], "running")
        self.assertIsInstance(meta["retry_in_ms"], int)
        self.assertGreaterEqual(meta["retry_in_ms"], 100)

        process_session("remove", session_id=session_id)

    def test_process_scope_isolation(self) -> None:
        cmd = 'python -c "import time;time.sleep(2)"'
        out_a = exec_command(cmd, background=True, scope="scope-a")
        out_b = exec_command(cmd, background=True, scope="scope-b")
        sid_a_match = re.search(r"session ([0-9a-f-]+)", out_a)
        sid_b_match = re.search(r"session ([0-9a-f-]+)", out_b)
        self.assertIsNotNone(sid_a_match)
        self.assertIsNotNone(sid_b_match)
        sid_a = sid_a_match.group(1) if sid_a_match else ""
        sid_b = sid_b_match.group(1) if sid_b_match else ""

        list_a = process_session("list", scope="scope-a")
        self.assertIn(sid_a, list_a)
        self.assertNotIn(sid_b, list_a)

        wrong_scope_poll = process_session("poll", session_id=sid_a, scope="scope-b")
        self.assertIn("No session found", wrong_scope_poll)

        self.assertIn("Removed session", process_session("remove", session_id=sid_a, scope="scope-a"))
        self.assertIn("Removed session", process_session("remove", session_id=sid_b, scope="scope-b"))

    def test_process_remove_running_session_hides_lifecycle_immediately(self) -> None:
        cmd = 'python -c "import time;time.sleep(5)"'
        out = exec_command(cmd, background=True)
        matched = re.search(r"session ([0-9a-f-]+)", out)
        self.assertIsNotNone(matched)
        session_id = matched.group(1) if matched else ""

        removed = process_session("remove", session_id=session_id)
        self.assertIn("Removed session", removed)

        listing = process_session("list")
        self.assertNotIn(session_id, listing)

        poll = process_session("poll", session_id=session_id)
        self.assertIn("No session found", poll)

    def test_exec_background_kill_sets_killed_status(self) -> None:
        cmd = 'python -c "import time;print(\'start\');time.sleep(10)"'
        out = exec_command(cmd, background=True)
        matched = re.search(r"session ([0-9a-f-]+)", out)
        self.assertIsNotNone(matched)
        session_id = matched.group(1) if matched else ""

        kill_out = process_session("kill", session_id=session_id)
        self.assertIn("Termination requested", kill_out)

        deadline = time.time() + 3
        last_poll = ""
        while time.time() < deadline:
            last_poll = process_session("poll", session_id=session_id, timeout_ms=200)
            if "Process was killed." in last_poll:
                break
        self.assertIn("Process was killed.", last_poll)

        listing = process_session("list")
        self.assertIn(session_id, listing)
        self.assertIn("killed", listing.lower())

        removed = process_session("remove", session_id=session_id)
        self.assertIn("Removed session", removed)

    def test_exec_tool_supports_shell_compound_command(self) -> None:
        if os.name == "nt":
            cmd = "set OPENPPX_EXEC_TEST=hello && echo %OPENPPX_EXEC_TEST%"
        else:
            cmd = "export OPENPPX_EXEC_TEST=hello && echo $OPENPPX_EXEC_TEST"
        out = exec_command(cmd)
        self.assertIn("hello", out.lower())

    def test_exec_tool_respects_allowlist(self) -> None:
        os.environ["OPENPPX_EXEC_ALLOWLIST"] = "python"
        out = exec_command("echo hello")
        self.assertIn("allowlist", out.lower())

    def test_exec_tool_allowlist_checks_all_chain_segments(self) -> None:
        os.environ["OPENPPX_EXEC_ALLOWLIST"] = "echo"
        out = exec_command("echo ok && python -V")
        self.assertIn("allowlist", out.lower())
        self.assertIn("python", out.lower())

    def test_exec_tool_allowlist_allows_builtin_plus_allowed_command(self) -> None:
        os.environ["OPENPPX_EXEC_ALLOWLIST"] = "echo"
        if os.name == "nt":
            cmd = "set OPENPPX_EXEC_TEST=hello && echo %OPENPPX_EXEC_TEST%"
        else:
            cmd = "export OPENPPX_EXEC_TEST=hello && echo $OPENPPX_EXEC_TEST"
        out = exec_command(cmd)
        self.assertIn("hello", out.lower())

    def test_exec_tool_allowlist_handles_env_assignment_prefix(self) -> None:
        os.environ["OPENPPX_EXEC_ALLOWLIST"] = "echo"
        if os.name == "nt":
            cmd = "set OPENPPX_EXEC_TEST=hello && echo %OPENPPX_EXEC_TEST%"
        else:
            cmd = "OPENPPX_EXEC_TEST=hello echo hello"
        out = exec_command(cmd)
        self.assertIn("hello", out.lower())

    def test_exec_tool_security_mode_deny_blocks_execution(self) -> None:
        os.environ["OPENPPX_EXEC_SECURITY"] = "deny"
        out = exec_command("echo hello")
        self.assertIn("mode=deny", out.lower())

    def test_exec_tool_security_mode_full_ignores_allowlist(self) -> None:
        os.environ["OPENPPX_EXEC_ALLOWLIST"] = "python"
        os.environ["OPENPPX_EXEC_SECURITY"] = "full"
        out = exec_command("echo hello")
        self.assertIn("hello", out.lower())

    def test_exec_tool_allowlist_mode_allows_safe_bins(self) -> None:
        os.environ["OPENPPX_EXEC_ALLOWLIST"] = ""
        os.environ["OPENPPX_EXEC_SECURITY"] = "allowlist"
        os.environ["OPENPPX_EXEC_SAFE_BINS"] = "echo"
        out = exec_command("echo hello")
        self.assertIn("hello", out.lower())

    def test_exec_tool_rejects_invalid_security_mode(self) -> None:
        os.environ["OPENPPX_EXEC_SECURITY"] = "invalid"
        out = exec_command("echo hello")
        self.assertIn("invalid openppx_exec_security", out.lower())

    def test_exec_tool_rejects_invalid_ask_mode(self) -> None:
        os.environ["OPENPPX_EXEC_ASK"] = "invalid"
        out = exec_command("echo hello")
        self.assertIn("invalid openppx_exec_ask", out.lower())

    def test_exec_tool_ask_always_requires_approval(self) -> None:
        os.environ["OPENPPX_EXEC_ASK"] = "always"
        out = exec_command("echo hello")
        self.assertIn("approval required", out.lower())
        self.assertIn("ask=always", out.lower())

    def test_exec_tool_ask_always_allows_confirmed_tool_context(self) -> None:
        os.environ["OPENPPX_EXEC_ASK"] = "always"
        tool_context = pytypes.SimpleNamespace(tool_confirmation=pytypes.SimpleNamespace(confirmed=True))
        out = exec_command("echo hello", tool_context=tool_context)
        self.assertIn("hello", out.lower())

    def test_exec_tool_ask_on_miss_requires_approval_for_allowlist_miss(self) -> None:
        os.environ["OPENPPX_EXEC_SECURITY"] = "allowlist"
        os.environ["OPENPPX_EXEC_ALLOWLIST"] = "python"
        os.environ["OPENPPX_EXEC_ASK"] = "on-miss"
        out = exec_command("echo hello")
        self.assertIn("approval required", out.lower())
        self.assertIn("ask=on-miss", out.lower())

    def test_exec_tool_ask_on_miss_allows_confirmed_allowlist_miss(self) -> None:
        os.environ["OPENPPX_EXEC_SECURITY"] = "allowlist"
        os.environ["OPENPPX_EXEC_ALLOWLIST"] = "python"
        os.environ["OPENPPX_EXEC_ASK"] = "on-miss"
        tool_context = pytypes.SimpleNamespace(tool_confirmation=pytypes.SimpleNamespace(confirmed=True))
        out = exec_command("echo hello", tool_context=tool_context)
        self.assertIn("hello", out.lower())

    def test_exec_tool_ask_on_miss_allows_allowlist_hit(self) -> None:
        os.environ["OPENPPX_EXEC_SECURITY"] = "allowlist"
        os.environ["OPENPPX_EXEC_ALLOWLIST"] = "echo"
        os.environ["OPENPPX_EXEC_ASK"] = "on-miss"
        out = exec_command("echo hello")
        self.assertIn("hello", out.lower())

    def test_exec_tool_is_disabled_when_allow_exec_is_off(self) -> None:
        os.environ["OPENPPX_ALLOW_EXEC"] = "0"
        out = exec_command("echo hello")
        self.assertIn("disabled by security policy", out.lower())

    def test_file_tools_respect_workspace_restriction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            os.environ["OPENPPX_RESTRICT_TO_WORKSPACE"] = "1"
            out = write_file("../outside.txt", "nope")
            self.assertIn("outside workspace", out.lower())

    def test_exec_tool_chain_path_guard_blocks_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            os.environ["OPENPPX_RESTRICT_TO_WORKSPACE"] = "1"
            out = exec_command("echo ok;../outside.sh")
            self.assertIn("outside workspace", out.lower())

    def test_message_tool_writes_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            response = message("hi", channel="local", chat_id="u1")
            self.assertIn("Message recorded", response)
            outbox = Path(tmp) / "messages" / "outbox.log"
            self.assertTrue(outbox.exists())

    def test_message_tool_uses_route_context_when_target_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            with route_context("telegram", "u2"):
                response = message("hi-context", channel=None, chat_id=None)
            self.assertIn("Message recorded", response)
            outbox = Path(tmp) / "messages" / "outbox.log"
            record = json.loads(outbox.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["channel"], "telegram")
            self.assertEqual(record["chat_id"], "u2")

    def test_message_tool_records_media_and_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            image_path = Path(tmp) / "tmp" / "demo.png"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

            response = message(
                "approve?",
                channel="local",
                chat_id="u1",
                media=["tmp/demo.png"],
                buttons=[["Approve", "Reject"]],
            )

            self.assertIn("Message recorded", response)
            self.assertIn("1 attachment", response)
            self.assertIn("2 button", response)
            outbox = Path(tmp) / "messages" / "outbox.log"
            record = json.loads(outbox.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["metadata"]["buttons"], [["Approve", "Reject"]])
            self.assertEqual(record["metadata"]["content_type"], "image")
            self.assertEqual(Path(record["metadata"]["media"][0]).resolve(), image_path.resolve())

    def test_message_image_tool_writes_image_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            image_path = Path(tmp) / "tmp" / "demo.png"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

            response = message_image("tmp/demo.png", caption="done", channel="feishu", chat_id="oc_1")
            self.assertIn("Image message recorded", response)
            outbox = Path(tmp) / "messages" / "outbox.log"
            record = json.loads(outbox.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["channel"], "feishu")
            self.assertEqual(record["chat_id"], "oc_1")
            self.assertEqual(record["content"], "done")
            self.assertEqual(record["metadata"]["content_type"], "image")
            self.assertEqual(Path(record["metadata"]["image_path"]).resolve(), image_path.resolve())

    def test_message_file_tool_writes_file_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            file_path = Path(tmp) / "tmp" / "report.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("done", encoding="utf-8")

            response = message_file("tmp/report.txt", caption="see attachment", channel="feishu", chat_id="oc_2")
            self.assertIn("File message recorded", response)
            outbox = Path(tmp) / "messages" / "outbox.log"
            record = json.loads(outbox.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["channel"], "feishu")
            self.assertEqual(record["chat_id"], "oc_2")
            self.assertEqual(record["content"], "see attachment")
            self.assertEqual(record["metadata"]["content_type"], "file")
            self.assertEqual(record["metadata"]["file_name"], "report.txt")
            self.assertEqual(Path(record["metadata"]["file_path"]).resolve(), file_path.resolve())

    def test_message_file_tool_rejects_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            response = message_file("tmp/missing.txt", channel="feishu", chat_id="oc_2")
            self.assertIn("Error: File not found", response)

    def test_cron_tool_add_list_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            with route_context("telegram", "u2"):
                create = cron(action="add", message="remind me", every_seconds=30)
            self.assertIn("Created job", create)
            store_path = Path(tmp) / ".openppx" / "cron_jobs.json"
            self.assertTrue(store_path.exists())
            payload = json.loads(store_path.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("version"), 3)
            self.assertTrue(payload.get("jobs"))
            self.assertEqual(payload.get("history"), [])
            first = payload["jobs"][0]
            self.assertTrue(first["payload"]["deliver"])
            self.assertEqual(first["payload"]["channel"], "telegram")
            self.assertEqual(first["payload"]["to"], "u2")
            self.assertEqual(first["payload"]["message"], "message from cron task: remind me")

            listing = cron(action="list")
            self.assertIn("Scheduled jobs", listing)
            self.assertIn("every:30s", listing)

            job_id = create.split("(id: ", 1)[1].rstrip(")")
            removed = cron(action="remove", job_id=job_id)
            self.assertIn("Removed job", removed)

    def test_browser_tool_open_snapshot_and_act_flow(self) -> None:
        started = json.loads(browser(action="start"))
        self.assertTrue(started["running"])

        opened = json.loads(browser(action="open", target_url="https://example.com"))
        self.assertTrue(opened["ok"])
        target_id = opened["targetId"]

        focused = json.loads(browser(action="focus", target_id=target_id))
        self.assertTrue(focused["ok"])
        self.assertTrue(focused["focused"])

        tabs = json.loads(browser(action="tabs"))
        self.assertTrue(tabs["running"])
        self.assertEqual(len(tabs["tabs"]), 1)
        self.assertEqual(tabs["tabs"][0]["targetId"], target_id)

        snapshot = json.loads(browser(action="snapshot", target_id=target_id, snapshot_format="ai"))
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["targetId"], target_id)
        self.assertIn("snapshot", snapshot)

        navigated = json.loads(browser(action="navigate", target_id=target_id, target_url="https://example.org"))
        self.assertTrue(navigated["ok"])
        self.assertIn("example.org", navigated["url"])

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_BROWSER_ARTIFACT_ROOT"] = tmp
            shot_path = Path(tmp) / "shots" / "shot.png"
            screenshot = json.loads(
                browser(
                    action="screenshot",
                    target_id=target_id,
                    screenshot_path=str(shot_path),
                    screenshot_type="jpeg",
                )
            )
            self.assertTrue(screenshot["ok"])
            self.assertEqual(screenshot["targetId"], target_id)
            self.assertTrue(screenshot["imageBase64"])
            self.assertEqual(screenshot["type"], "jpeg")
            self.assertIn("jpeg", screenshot["contentType"])
            self.assertEqual(Path(screenshot["path"]).resolve(), shot_path.resolve())
            self.assertTrue(shot_path.exists())

            os.environ["OPENPPX_BROWSER_ARTIFACT_ROOT"] = tmp
            pdf_path = Path(tmp) / "pdfs" / "shot.pdf"
            pdf = json.loads(
                browser(
                    action="pdf",
                    target_id=target_id,
                    pdf_path=str(pdf_path),
                )
            )
            self.assertTrue(pdf["ok"])
            self.assertEqual(Path(pdf["path"]).resolve(), pdf_path.resolve())
            self.assertTrue(pdf_path.exists())

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_BROWSER_ARTIFACT_ROOT"] = tmp
            console_path = Path(tmp) / "console" / "tool.json"
            console = json.loads(
                browser(
                    action="console",
                    target_id=target_id,
                    console_level="info",
                    console_path=str(console_path),
                )
            )
            self.assertTrue(console["ok"])
            self.assertIn("messages", console)
            self.assertTrue(console["messages"])
            self.assertEqual(console["messages"][0]["level"], "info")
            self.assertEqual(Path(console["path"]).resolve(), console_path.resolve())
            self.assertTrue(console_path.exists())

    def test_browser_tool_blocks_pdf_outside_artifact_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            os.environ["OPENPPX_BROWSER_ARTIFACT_ROOT"] = root_tmp
            json.loads(browser(action="start"))
            opened = json.loads(browser(action="open", target_url="https://example.com"))
            target_id = opened["targetId"]
            outside_pdf = Path(outside_tmp) / "outside.pdf"
            payload = json.loads(
                browser(
                    action="pdf",
                    target_id=target_id,
                    pdf_path=str(outside_pdf),
                )
            )
            self.assertFalse(payload["ok"])
            self.assertIn("outside artifact root", payload["error"])

    def test_browser_tool_blocks_screenshot_outside_artifact_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            os.environ["OPENPPX_BROWSER_ARTIFACT_ROOT"] = root_tmp
            json.loads(browser(action="start"))
            opened = json.loads(browser(action="open", target_url="https://example.com"))
            target_id = opened["targetId"]
            outside_png = Path(outside_tmp) / "outside.png"
            payload = json.loads(
                browser(
                    action="screenshot",
                    target_id=target_id,
                    screenshot_path=str(outside_png),
                )
            )
            self.assertFalse(payload["ok"])
            self.assertIn("outside artifact root", payload["error"])

    def test_browser_tool_blocks_console_export_outside_artifact_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            os.environ["OPENPPX_BROWSER_ARTIFACT_ROOT"] = root_tmp
            json.loads(browser(action="start"))
            opened = json.loads(browser(action="open", target_url="https://example.com"))
            target_id = opened["targetId"]
            outside_json = Path(outside_tmp) / "outside.json"
            payload = json.loads(
                browser(
                    action="console",
                    target_id=target_id,
                    console_level="info",
                    console_path=str(outside_json),
                )
            )
            self.assertFalse(payload["ok"])
            self.assertIn("outside artifact root", payload["error"])

        with tempfile.TemporaryDirectory() as tmp:
            upload_file = Path(tmp) / "upload.txt"
            upload_file.write_text("demo", encoding="utf-8")
            os.environ["OPENPPX_BROWSER_UPLOAD_ROOT"] = tmp
            uploaded = json.loads(
                browser(
                    action="upload",
                    target_id=target_id,
                    paths=[str(upload_file)],
                    ref="#file-input",
                )
            )
        self.assertTrue(uploaded["ok"])
        self.assertEqual(uploaded["uploadedPaths"], [str(upload_file.resolve())])

        dialog = json.loads(
            browser(
                action="dialog",
                target_id=target_id,
                accept=True,
                prompt_text="confirm",
            )
        )
        self.assertTrue(dialog["ok"])
        self.assertTrue(dialog["armed"])

        acted = json.loads(
            browser(
                action="act",
                target_id=target_id,
                request=json.dumps({"kind": "type", "ref": "e2", "text": "hello"}),
            )
        )
        self.assertTrue(acted["ok"])
        self.assertEqual(acted["kind"], "type")

        acted_with_selector = json.loads(
            browser(
                action="act",
                target_id=target_id,
                request=json.dumps({"kind": "click", "selector": "button.primary"}),
            )
        )
        self.assertTrue(acted_with_selector["ok"])
        self.assertEqual(acted_with_selector["kind"], "click")

        hovered = json.loads(
            browser(
                action="act",
                target_id=target_id,
                request=json.dumps({"kind": "hover", "ref": "e1"}),
            )
        )
        self.assertTrue(hovered["ok"])
        self.assertEqual(hovered["kind"], "hover")

        selected = json.loads(
            browser(
                action="act",
                target_id=target_id,
                request=json.dumps({"kind": "select", "ref": "e2", "values": ["v1"]}),
            )
        )
        self.assertTrue(selected["ok"])
        self.assertEqual(selected["kind"], "select")

        evaluated = json.loads(
            browser(
                action="act",
                target_id=target_id,
                request=json.dumps({"kind": "evaluate", "fn": "() => 1"}),
            )
        )
        self.assertTrue(evaluated["ok"])
        self.assertEqual(evaluated["kind"], "evaluate")

        filled = json.loads(
            browser(
                action="act",
                target_id=target_id,
                request=json.dumps(
                    {"kind": "fill", "fields": [{"ref": "e2", "text": "abc"}]}
                ),
            )
        )
        self.assertTrue(filled["ok"])
        self.assertEqual(filled["kind"], "fill")

        resized = json.loads(
            browser(
                action="act",
                target_id=target_id,
                request=json.dumps({"kind": "resize", "width": 1024, "height": 768}),
            )
        )
        self.assertTrue(resized["ok"])
        self.assertEqual(resized["kind"], "resize")

        dragged = json.loads(
            browser(
                action="act",
                target_id=target_id,
                request=json.dumps({"kind": "drag", "startRef": "e1", "endRef": "e2"}),
            )
        )
        self.assertTrue(dragged["ok"])
        self.assertEqual(dragged["kind"], "drag")

        closed = json.loads(browser(action="close", target_id=target_id))
        self.assertTrue(closed["ok"])
        self.assertTrue(closed["closed"])

        invalid_request = json.loads(browser(action="act", request="{not-json"))
        self.assertFalse(invalid_request["ok"])
        self.assertIn("valid JSON object string", invalid_request["error"])

    def test_browser_tool_reports_errors_for_missing_inputs(self) -> None:
        missing_url = json.loads(browser(action="open"))
        self.assertFalse(missing_url["ok"])
        self.assertIn("url", missing_url["error"])

        missing_request = json.loads(browser(action="act"))
        self.assertFalse(missing_request["ok"])
        self.assertIn("kind", missing_request["error"])

        json.loads(browser(action="start"))
        json.loads(browser(action="open", target_url="https://example.com"))
        missing_select_values = json.loads(
            browser(action="act", request=json.dumps({"kind": "select", "ref": "e1"}))
        )
        self.assertFalse(missing_select_values["ok"])
        self.assertIn("values", missing_select_values["error"])

        missing_fill_fields = json.loads(
            browser(action="act", request=json.dumps({"kind": "fill"}))
        )
        self.assertFalse(missing_fill_fields["ok"])
        self.assertIn("fields", missing_fill_fields["error"])

        missing_navigate_url = json.loads(browser(action="navigate", target_id="tab-x"))
        self.assertFalse(missing_navigate_url["ok"])
        self.assertIn("url", missing_navigate_url["error"])

        missing_upload_paths = json.loads(browser(action="upload", target_id="tab-x"))
        self.assertFalse(missing_upload_paths["ok"])
        self.assertIn("paths", missing_upload_paths["error"])

        missing_dialog_accept = json.loads(browser(action="dialog", target_id="tab-x"))
        self.assertFalse(missing_dialog_accept["ok"])
        self.assertIn("accept", missing_dialog_accept["error"])

        invalid_screenshot_type = json.loads(browser(action="screenshot", screenshot_type="gif"))
        self.assertFalse(invalid_screenshot_type["ok"])
        self.assertIn("image_type", invalid_screenshot_type["error"])

    def test_browser_tool_reports_runtime_errors(self) -> None:
        not_running = json.loads(browser(action="snapshot"))
        self.assertFalse(not_running["ok"])
        self.assertIn("not running", not_running["error"])
        self.assertEqual(not_running["status"], 409)

    def test_browser_upload_requests_heartbeat_wake_on_success(self) -> None:
        class _DummyService:
            def dispatch(self, _request):
                return BrowserDispatchResponse(200, {"ok": True})

        reasons: list[str] = []
        configure_heartbeat_waker(reasons.append)
        with patch("openppx.tooling.registry.get_browser_control_service", return_value=_DummyService()):
            payload = json.loads(browser(action="upload", paths=["tmp/a.txt"]))
        self.assertTrue(payload["ok"])
        self.assertIn("hook:upload", reasons)

    def test_browser_dialog_requests_heartbeat_wake_on_success(self) -> None:
        class _DummyService:
            def dispatch(self, _request):
                return BrowserDispatchResponse(200, {"ok": True})

        reasons: list[str] = []
        configure_heartbeat_waker(reasons.append)
        with patch("openppx.tooling.registry.get_browser_control_service", return_value=_DummyService()):
            payload = json.loads(browser(action="dialog", accept=True))
        self.assertTrue(payload["ok"])
        self.assertIn("hook:dialog", reasons)

    def test_browser_tool_supports_profiles_and_stop(self) -> None:
        profiles = json.loads(browser(action="profiles"))
        self.assertTrue(profiles["profiles"])
        names = {entry["name"] for entry in profiles["profiles"]}
        self.assertIn("openppx", names)
        self.assertIn("chrome", names)

        json.loads(browser(action="start"))
        json.loads(browser(action="open", target_url="https://example.com"))
        stopped = json.loads(browser(action="stop"))
        self.assertFalse(stopped["running"])
        self.assertEqual(stopped["tabCount"], 0)

    def test_browser_tool_profiles_attach_compatibility_aliases(self) -> None:
        class _FakeBrowserService:
            def dispatch(self, _request: object) -> BrowserDispatchResponse:
                return BrowserDispatchResponse(
                    200,
                    {
                        "profiles": [
                            {
                                "name": "openppx",
                                "attachMode": "launch-or-cdp",
                                "ownershipModel": {"browser": "owned"},
                                "requires": {"OPENPPX_BROWSER_CDP_URL": False},
                                "capability": {
                                    "backend": "playwright",
                                    "attachMode": "launch-or-cdp",
                                    "supportedActions": ["status", "snapshot"],
                                },
                            }
                        ]
                    },
                )

        with patch("openppx.tooling.registry.get_browser_control_service", return_value=_FakeBrowserService()):
            payload = json.loads(browser(action="profiles"))
        self.assertEqual(payload["profiles"][0]["attach_mode"], "launch-or-cdp")
        self.assertIn("ownership_model", payload["profiles"][0])
        self.assertIn("requirements", payload["profiles"][0])
        self.assertEqual(payload["profiles"][0]["capability"]["attach_mode"], "launch-or-cdp")
        self.assertEqual(payload["profiles"][0]["capability"]["supported_actions"], ["status", "snapshot"])

    def test_browser_tool_rejects_unsupported_profile_actions(self) -> None:
        unsupported = json.loads(browser(action="start", profile="chrome"))
        self.assertFalse(unsupported["ok"])
        self.assertEqual(unsupported["status"], 501)
        self.assertIn("not implemented", unsupported["error"])

    def test_browser_tool_auto_includes_browser_service_tokens(self) -> None:
        os.environ["OPENPPX_BROWSER_CONTROL_TOKEN"] = "token-3"
        os.environ["OPENPPX_BROWSER_MUTATION_TOKEN"] = "mut-3"
        configure_browser_runtime(None)

        started = json.loads(browser(action="start"))
        self.assertTrue(started["running"])

        opened = json.loads(browser(action="open", target_url="https://example.com"))
        self.assertTrue(opened["ok"])

    def test_browser_tool_exposes_target_routing_errors(self) -> None:
        unsupported = json.loads(browser(action="status", target="sandbox"))
        self.assertFalse(unsupported["ok"])
        self.assertEqual(unsupported["status"], 501)
        self.assertIn("not implemented", unsupported["error"])

        invalid = json.loads(browser(action="status", target="invalid"))
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["status"], 400)
        self.assertIn("target must be", invalid["error"])

    def test_browser_tool_routes_node_target_to_proxy_when_configured(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_NODE_PROXY_TOKEN"] = "node-token"

        captured: dict[str, str] = {}

        def _fake_urlopen(req, timeout=20):
            captured["url"] = req.full_url
            captured["token"] = req.headers.get("X-openppx-browser-proxy-token", "")
            captured["timeout"] = str(timeout)
            return _DummyResponse('{"ok": true, "via": "node-proxy"}')

        with patch("openppx.tooling.registry.urlopen", side_effect=_fake_urlopen):
            payload = json.loads(browser(action="status", target="node", node="node-1", timeout_ms=3500))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["via"], "node-proxy")
        self.assertIn("proxy.local:8787", captured["url"])
        self.assertIn("node=node-1", captured["url"])
        self.assertIn("timeoutMs=3500", captured["url"])
        self.assertEqual(captured["timeout"], "3.5")
        self.assertEqual(captured["token"], "node-token")

    def test_browser_tool_records_remote_provider_capability(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://user:secret@proxy.local:8787"
            os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
                {"capability": {"backend": "node-proxy", "supportedActions": ["status", "snapshot"]}}
            )
            with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
                payload = json.loads(browser(action="status", target="node", node="node-1"))

            providers = json.loads(list_browser_remote_providers(target="node"))

            self.assertTrue(payload["ok"])
            self.assertIn("provider", payload)
            self.assertTrue(providers["ok"])
            self.assertEqual(len(providers["items"]), 1)
            item = providers["items"][0]
            self.assertEqual(item["target"], "node")
            self.assertEqual(item["node"], "node-1")
            self.assertEqual(item["status"], "available")
            self.assertEqual(item["proxy_url"], "http://proxy.local:8787")
            self.assertEqual(item["capability"]["backend"], "node-proxy")
            self.assertEqual(item["capability"]["supported_actions"], ["status", "snapshot"])

    def test_browser_tool_records_remote_job_when_proxy_declares_job_id(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
            os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
                {"capability": {"backend": "node-proxy", "supportedActions": ["act", "status"]}}
            )
            proxy_payload = json.dumps(
                {
                    "ok": True,
                    "jobId": "remote-job-1",
                    "jobStatus": "in_progress",
                    "summary": "Remote browser job started.",
                }
            )
            with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse(proxy_payload)):
                payload = json.loads(
                    browser(
                        action="act",
                        target="node",
                        node="node-1",
                        request=json.dumps({"goal": "fill form"}),
                    )
                )

            jobs = json.loads(list_browser_remote_jobs(target="node", status="running"))

            self.assertTrue(payload["ok"])
            self.assertIn("remote_job", payload)
            self.assertEqual(payload["remote_job"]["external_job_id"], "remote-job-1")
            self.assertEqual(payload["remote_job"]["status"], "running")
            self.assertIn("remote_job_task_id", payload)
            task = TaskStore().get_task(payload["remote_job_task_id"])
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.kind, "browser_remote")
            self.assertEqual(task.status, "running")
            self.assertEqual(task.runner_payload["runner"], "browser_remote")
            self.assertEqual(task.runner_payload["job_record_id"], payload["remote_job"]["job_record_id"])
            self.assertEqual(task.runner_payload["external_job_id"], "remote-job-1")
            self.assertTrue(payload["remote_job_task"]["controls"]["can_resume"])
            self.assertFalse(payload["remote_job_task"]["controls"]["can_cancel"])
            self.assertTrue(jobs["ok"])
            self.assertEqual(len(jobs["items"]), 1)
            item = jobs["items"][0]
            self.assertEqual(item["target"], "node")
            self.assertEqual(item["node"], "node-1")
            self.assertEqual(item["action"], "act")
            self.assertEqual(item["external_job_id"], "remote-job-1")
            self.assertEqual(item["payload"]["summary"], "Remote browser job started.")

    def test_browser_tool_materializes_remote_job_protocol_controls(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_TASK_DB_PATH"] = str(Path(tmp) / "tasks.db")
            os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
            os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
                {
                    "capability": {
                        "backend": "node-proxy",
                        "supportedActions": ["act", "status"],
                        "jobProtocol": {
                            "statusPath": "/jobs/{job_id}",
                            "outputPath": "/jobs/{job_id}/output",
                            "cancelPath": "/jobs/{job_id}/cancel",
                        },
                    }
                }
            )
            with patch(
                "openppx.tooling.registry.urlopen",
                return_value=_DummyResponse(
                    json.dumps(
                        {
                            "ok": True,
                            "jobId": "remote-job-1",
                            "jobStatus": "running",
                            "summary": "Remote browser job started.",
                        }
                    )
                ),
            ):
                payload = json.loads(
                    browser(
                        action="act",
                        target="node",
                        node="node-1",
                        request=json.dumps({"goal": "fill form"}),
                    )
                )

            task = TaskStore().get_task(payload["remote_job_task_id"])

            self.assertIsNotNone(task)
            assert task is not None
            self.assertTrue(payload["remote_job_task"]["controls"]["can_cancel"])
            self.assertEqual(payload["remote_job_task"]["controls"]["cancel_tool"], "cancel_task")
            self.assertEqual(task.runner_capabilities["cancel"], True)
            self.assertEqual(task.runner_payload["job_protocol"]["cancel_path"], "/jobs/{job_id}/cancel")

    def test_browser_tool_routes_sandbox_target_to_proxy_when_configured(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_SANDBOX_PROXY_URL"] = "http://sandbox-proxy.local:9797"
        os.environ["OPENPPX_BROWSER_PROXY_TOKEN"] = "shared-token"

        captured: dict[str, str] = {}

        def _fake_urlopen(req, timeout=20):
            captured["url"] = req.full_url
            captured["token"] = req.headers.get("X-openppx-browser-proxy-token", "")
            return _DummyResponse('{"ok": true, "via": "sandbox-proxy"}')

        with patch("openppx.tooling.registry.urlopen", side_effect=_fake_urlopen):
            payload = json.loads(browser(action="status", target="sandbox"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["via"], "sandbox-proxy")
        self.assertIn("sandbox-proxy.local:9797", captured["url"])
        self.assertEqual(captured["token"], "shared-token")

    def test_browser_tool_blocks_unsupported_action_by_proxy_capability(self) -> None:
        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "snapshot"]}}
        )
        with patch("openppx.tooling.registry.urlopen") as mocked_urlopen:
            payload = json.loads(browser(action="pdf", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 501)
        self.assertIn("not supported", payload["error"])
        self.assertIn("status", payload["supportedActions"])
        self.assertIn("action=status", payload["hint"])
        mocked_urlopen.assert_not_called()

    def test_browser_tool_unsupported_action_includes_capability_warnings(self) -> None:
        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {
                "capability": {
                    "supportedActions": ["status"],
                    "errorCodes": "bad-shape",
                }
            }
        )
        with patch("openppx.tooling.registry.urlopen") as mocked_urlopen:
            payload = json.loads(browser(action="pdf", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 501)
        self.assertIn("capabilityWarnings", payload)
        self.assertIn("errorCodes", payload["capabilityWarnings"][0])
        mocked_urlopen.assert_not_called()

    def test_browser_tool_allows_supported_action_by_proxy_capability(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"supportedActions": ["status", "snapshot"]}
        )
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')) as mocked_urlopen:
            payload = json.loads(browser(action="status", target="node"))
        self.assertTrue(payload["ok"])
        mocked_urlopen.assert_called_once()

    def test_browser_tool_injects_proxy_capability_into_response(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"backend": "node-proxy", "attachMode": "remote", "supportedActions": ["status"]}}
        )
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
            payload = json.loads(browser(action="status", target="node"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "node")
        self.assertEqual(payload["capability"]["backend"], "node-proxy")
        self.assertEqual(payload["capability"]["attach_mode"], "remote")

    def test_browser_tool_keeps_response_capability_if_proxy_already_provides(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"backend": "node-proxy", "supportedActions": ["status"]}}
        )
        with patch(
            "openppx.tooling.registry.urlopen",
            return_value=_DummyResponse('{"ok":true,"capability":{"backend":"proxy-inline","attachMode":"inline"}}'),
        ):
            payload = json.loads(browser(action="status", target="node"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["capability"]["backend"], "proxy-inline")
        self.assertEqual(payload["capability"]["attach_mode"], "inline")

    def test_browser_tool_status_includes_default_proxy_capability_schema(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
            payload = json.loads(browser(action="status", target="node"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "node")
        self.assertEqual(payload["capability"]["backend"], "node-proxy")
        self.assertEqual(payload["capability"]["supported_actions"], [])
        self.assertIn("proxy_timeout", payload["capability"]["error_codes"])

    def test_browser_tool_warns_on_invalid_proxy_capability_json(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = '{"capability":'
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
            payload = json.loads(browser(action="status", target="node"))
        self.assertTrue(payload["ok"])
        self.assertIn("capabilityWarnings", payload)
        self.assertIn("capability_warnings", payload)
        self.assertIn("invalid JSON", payload["capabilityWarnings"][0])

    def test_browser_tool_warns_on_invalid_proxy_error_codes_shape(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"backend": "node-proxy", "supportedActions": ["status"], "errorCodes": "bad-shape"}}
        )
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
            payload = json.loads(browser(action="status", target="node"))
        self.assertTrue(payload["ok"])
        self.assertIn("capabilityWarnings", payload)
        self.assertIn("errorCodes", payload["capabilityWarnings"][0])
        self.assertIn("proxy_timeout", payload["capability"]["error_codes"])

    def test_browser_tool_profiles_includes_capability_warnings_alias(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = '{"capability":'
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
            payload = json.loads(browser(action="profiles", target="node"))
        self.assertTrue(payload["ok"])
        self.assertIn("capabilityWarnings", payload)
        self.assertIn("capability_warnings", payload)
        self.assertTrue(isinstance(payload.get("profiles"), list))

    def test_browser_tool_status_exposes_recommended_actions_from_capability(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "snapshot", "tabs"]}}
        )
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
            payload = json.loads(browser(action="status", target="node"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["supportedActions"], ["status", "tabs", "snapshot"])
        self.assertEqual(payload["recommendedActions"], ["status", "tabs", "snapshot"])

    def test_browser_tool_recommended_actions_follow_priority_and_cap(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {
                "capability": {
                    "supportedActions": [
                        "pdf",
                        "status",
                        "dialog",
                        "snapshot",
                        "tabs",
                        "profiles",
                        "open",
                        "custom-z",
                    ]
                }
            }
        )
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
            payload = json.loads(browser(action="status", target="node"))
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["supportedActions"],
            ["status", "profiles", "tabs", "snapshot", "open", "pdf", "dialog", "custom-z"],
        )
        self.assertEqual(payload["recommendedActions"], ["status", "profiles", "tabs", "snapshot", "open"])

    def test_browser_tool_recommended_actions_limit_from_env(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_RECOMMENDED_ACTIONS_LIMIT"] = "2"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "profiles", "tabs", "snapshot"]}}
        )
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
            payload = json.loads(browser(action="status", target="node"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["recommendedActions"], ["status", "profiles"])

    def test_browser_tool_recommended_actions_limit_invalid_uses_default(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_RECOMMENDED_ACTIONS_LIMIT"] = "bad-value"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "profiles", "tabs", "snapshot", "open", "pdf"]}}
        )
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
            payload = json.loads(browser(action="status", target="node"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["recommendedActions"], ["status", "profiles", "tabs", "snapshot", "open"])

    def test_browser_tool_recommended_actions_order_from_env(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_RECOMMENDED_ACTIONS_ORDER_JSON"] = json.dumps(
            ["pdf", "snapshot", "status"]
        )
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "profiles", "snapshot", "pdf"]}}
        )
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
            payload = json.loads(browser(action="status", target="node"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["supportedActions"], ["pdf", "snapshot", "status", "profiles"])
        self.assertEqual(payload["recommendedActions"], ["pdf", "snapshot", "status", "profiles"])
        self.assertEqual(payload["capability"]["recommended_order"], ["pdf", "snapshot", "status"])

    def test_browser_tool_recommended_actions_order_invalid_uses_default(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_RECOMMENDED_ACTIONS_ORDER_JSON"] = '{"bad":"shape"}'
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "profiles", "snapshot"]}}
        )
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
            payload = json.loads(browser(action="status", target="node"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["supportedActions"], ["status", "profiles", "snapshot"])

    def test_browser_tool_profiles_includes_default_proxy_schema(self) -> None:
        class _DummyResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload.encode("utf-8")

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_SANDBOX_PROXY_URL"] = "http://sandbox-proxy.local:9797"
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse('{"ok":true}')):
            payload = json.loads(browser(action="profiles", target="sandbox"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "sandbox")
        self.assertEqual(payload["capability"]["backend"], "sandbox-proxy")
        self.assertIn("proxy_timeout", payload["capability"]["error_codes"])
        self.assertEqual(payload["profiles"], [])

    def test_browser_tool_rejects_node_param_without_node_target(self) -> None:
        payload = json.loads(browser(action="status", target="host", node="node-1"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 400)
        self.assertIn('target="node"', payload["error"])

    def test_browser_tool_rejects_invalid_timeout_ms(self) -> None:
        payload = json.loads(browser(action="status", target="node", timeout_ms=0))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 400)
        self.assertIn("timeout_ms", payload["error"])

    def test_browser_tool_adds_profile_switch_hint_on_mismatch(self) -> None:
        class _DummyService:
            def dispatch(self, _request):
                return BrowserDispatchResponse(
                    status=409,
                    body={"ok": False, "error": "profile mismatch: active profile is openppx"},
                )

        with patch("openppx.tooling.registry.get_browser_control_service", return_value=_DummyService()):
            payload = json.loads(browser(action="status"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 409)
        self.assertEqual(payload["errorCode"], "browser_conflict")
        self.assertIn("hint", payload)
        self.assertIn("action=stop", payload["hint"])

    def test_browser_tool_preserves_existing_error_code_from_service(self) -> None:
        class _DummyService:
            def dispatch(self, _request):
                return BrowserDispatchResponse(
                    status=503,
                    body={
                        "ok": False,
                        "status": 503,
                        "error": "chrome relay timeout",
                        "errorCode": "relay_timeout",
                    },
                )

        with patch("openppx.tooling.registry.get_browser_control_service", return_value=_DummyService()):
            payload = json.loads(browser(action="status"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 503)
        self.assertEqual(payload["errorCode"], "relay_timeout")

    def test_browser_tool_handles_proxy_non_json_response(self) -> None:
        class _DummyResponse:
            def read(self) -> bytes:
                return b"not-json"

            def __enter__(self) -> "_DummyResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        with patch("openppx.tooling.registry.urlopen", return_value=_DummyResponse()):
            payload = json.loads(browser(action="status", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 502)
        self.assertEqual(payload["errorCode"], "proxy_invalid_json")
        self.assertIn("invalid proxy response", payload["error"])

    def test_browser_tool_uses_structured_proxy_error_payload(self) -> None:
        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        http_error = HTTPError(
            url="http://proxy.local:8787/",
            code=502,
            msg="Bad Gateway",
            hdrs=None,
            fp=BytesIO(b'{"error":"rate limited","status":429}'),
        )
        with patch("openppx.tooling.registry.urlopen", side_effect=http_error):
            payload = json.loads(browser(action="status", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 429)
        self.assertEqual(payload["error"], "rate limited")
        self.assertEqual(payload["errorCode"], "proxy_http_error")

    def test_browser_tool_maps_proxy_timeout_error(self) -> None:
        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        with patch("openppx.tooling.registry.urlopen", side_effect=URLError(TimeoutError("timed out"))):
            payload = json.loads(browser(action="status", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 504)
        self.assertEqual(payload["errorCode"], "proxy_timeout")
        self.assertIn("timeout", payload["error"])

    def test_browser_tool_maps_proxy_direct_timeout_error(self) -> None:
        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        with patch("openppx.tooling.registry.urlopen", side_effect=TimeoutError("timed out")):
            payload = json.loads(browser(action="status", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 504)
        self.assertEqual(payload["errorCode"], "proxy_timeout")

    def test_browser_tool_maps_proxy_connection_refused_error(self) -> None:
        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        with patch("openppx.tooling.registry.urlopen", side_effect=URLError(ConnectionRefusedError("refused"))):
            payload = json.loads(browser(action="status", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 503)
        self.assertEqual(payload["errorCode"], "proxy_connection_refused")
        self.assertIn("connection refused", payload["error"])

    def test_browser_tool_proxy_http_error_includes_target_capability(self) -> None:
        os.environ["OPENPPX_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENPPX_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"backend": "node-proxy", "supportedActions": ["status"]}}
        )
        http_error = HTTPError(
            url="http://proxy.local:8787/",
            code=502,
            msg="Bad Gateway",
            hdrs=None,
            fp=BytesIO(b'{"error":"failed","status":502}'),
        )
        with patch("openppx.tooling.registry.urlopen", side_effect=http_error):
            payload = json.loads(browser(action="status", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["target"], "node")
        self.assertEqual(payload["capability"]["backend"], "node-proxy")

    def test_browser_tool_proxy_url_error_includes_default_target_capability(self) -> None:
        os.environ["OPENPPX_BROWSER_SANDBOX_PROXY_URL"] = "http://sandbox-proxy.local:9797"
        with patch("openppx.tooling.registry.urlopen", side_effect=URLError(TimeoutError("timed out"))):
            payload = json.loads(browser(action="status", target="sandbox"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["target"], "sandbox")
        self.assertEqual(payload["capability"]["backend"], "sandbox-proxy")
        self.assertEqual(payload["capability"]["supported_actions"], [])

    def test_browser_tool_blocks_private_navigation_by_default(self) -> None:
        json.loads(browser(action="start"))
        blocked = json.loads(browser(action="open", target_url="http://127.0.0.1:9222"))
        self.assertFalse(blocked["ok"])
        self.assertIn("blocked by policy", blocked["error"])

    def test_browser_tool_allows_private_navigation_when_disabled(self) -> None:
        os.environ["OPENPPX_BROWSER_BLOCK_PRIVATE_NETWORKS"] = "0"
        configure_browser_runtime(None)
        json.loads(browser(action="start"))
        opened = json.loads(browser(action="open", target_url="http://127.0.0.1:9222"))
        self.assertTrue(opened["ok"])

    def test_browser_tool_blocks_upload_outside_upload_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            outside_file = Path(outside_tmp) / "upload.txt"
            outside_file.write_text("demo", encoding="utf-8")
            os.environ["OPENPPX_BROWSER_UPLOAD_ROOT"] = root_tmp
            configure_browser_runtime(None)
            json.loads(browser(action="start"))
            json.loads(browser(action="open", target_url="https://example.com"))
            blocked = json.loads(browser(action="upload", paths=[str(outside_file)]))
            self.assertFalse(blocked["ok"])
            self.assertIn("outside upload root", blocked["error"])

    def test_web_fetch_rejects_invalid_url(self) -> None:
        payload = json.loads(web_fetch("file:///tmp/test.txt"))
        self.assertIn("error", payload)

    def test_web_fetch_blocks_private_hosts_by_default(self) -> None:
        payload = json.loads(web_fetch("http://127.0.0.1:8080"))
        self.assertIn("error", payload)
        self.assertIn("blocked", payload["error"].lower())

    def test_web_fetch_blocks_private_redirect_target(self) -> None:
        class _FakeResponse(BytesIO):
            status = 200
            headers = {"Content-Type": "text/html"}
            url = "http://127.0.0.1:8080/private"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch(
            "openppx.tooling.registry.urlopen",
            side_effect=[URLError("fallback"), _FakeResponse(b"<html>redirected</html>")],
        ):
            payload = json.loads(web_fetch("https://example.com"))

        self.assertIn("error", payload)
        self.assertIn("blocked", payload["error"].lower())

    def test_web_fetch_prefers_jina_reader_payload(self) -> None:
        class _FakeResponse(BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}
            url = "https://r.jina.ai/https://example.com"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        payload = {
            "data": {
                "url": "https://example.com/final",
                "title": "Example",
                "content": "Hello from Jina",
            }
        }
        with patch("openppx.tooling.registry.urlopen", return_value=_FakeResponse(json.dumps(payload).encode("utf-8"))):
            result = json.loads(web_fetch("https://example.com"))

        self.assertEqual(result["extractor"], "jina")
        self.assertTrue(result["untrusted"])
        self.assertIn("Hello from Jina", result["text"])

    def test_web_fetch_detects_image_payload(self) -> None:
        class _FakeImageResponse(BytesIO):
            status = 200
            headers = {"Content-Type": "image/png"}
            url = "https://example.com/image.png"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch(
            "openppx.tooling.registry.urlopen",
            side_effect=[URLError("fallback"), _FakeImageResponse(b"\x89PNG\r\n")],
        ):
            result = json.loads(web_fetch("https://example.com/image.png"))

        self.assertEqual(result["extractor"], "image")
        self.assertEqual(result["mimeType"], "image/png")
        self.assertTrue(result["untrusted"])

    def test_web_tools_respect_security_network_flag(self) -> None:
        os.environ["OPENPPX_ALLOW_NETWORK"] = "0"
        search_out = web_search("adk")
        fetch_payload = json.loads(web_fetch("https://example.com"))
        self.assertIn("disabled by security policy", search_out.lower())
        self.assertIn("disabled by security policy", fetch_payload["error"].lower())

    def test_web_search_respects_disabled_flag(self) -> None:
        os.environ["OPENPPX_WEB_ENABLED"] = "0"
        out = web_search("adk")
        self.assertIn("disabled", out.lower())

    def test_web_search_respects_provider_config(self) -> None:
        os.environ["OPENPPX_WEB_ENABLED"] = "1"
        os.environ["OPENPPX_WEB_SEARCH_ENABLED"] = "1"
        os.environ["OPENPPX_WEB_SEARCH_PROVIDER"] = "dummy"
        out = web_search("adk")
        self.assertIn("not supported", out.lower())

    def test_web_search_provider_argument_overrides_config(self) -> None:
        os.environ["OPENPPX_WEB_ENABLED"] = "1"
        os.environ["OPENPPX_WEB_SEARCH_ENABLED"] = "1"
        os.environ["OPENPPX_WEB_SEARCH_PROVIDER"] = "dummy"
        html_body = (
            '<a class="result__a" href="https://example.com">Example Title</a>'
            '<div class="result__snippet">Example snippet</div>'
        )

        class _FakeResponse(BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("openppx.tooling.registry.urlopen", return_value=_FakeResponse(html_body.encode("utf-8"))):
            out = web_search("adk", provider="duckduckgo")

        self.assertIn("Example Title", out)

    def test_web_search_falls_back_to_duckduckgo_when_brave_key_missing(self) -> None:
        os.environ["OPENPPX_WEB_ENABLED"] = "1"
        os.environ["OPENPPX_WEB_SEARCH_ENABLED"] = "1"
        os.environ["OPENPPX_WEB_SEARCH_PROVIDER"] = "brave"
        os.environ.pop("BRAVE_API_KEY", None)
        html_body = (
            '<a class="result__a" href="https://example.com">Example Title</a>'
            '<div class="result__snippet">Example snippet</div>'
        )

        class _FakeResponse(BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("openppx.tooling.registry.urlopen", return_value=_FakeResponse(html_body.encode("utf-8"))):
            out = web_search("adk")

        self.assertIn("Example Title", out)
        self.assertIn("https://example.com", out)

    def test_spawn_subagent_requires_dispatcher(self) -> None:
        ctx = pytypes.SimpleNamespace(
            user_id="u1",
            invocation_id="inv-1",
            function_call_id="fc-1",
            session=pytypes.SimpleNamespace(id="s1"),
        )
        out = spawn_subagent(prompt="run task", tool_context=ctx)
        self.assertEqual(out.get("status"), "error")
        self.assertIn("dispatcher", str(out.get("error", "")).lower())

    def test_spawn_subagent_dispatches_request(self) -> None:
        captured: list[SubagentSpawnRequest] = []
        configure_subagent_dispatcher(captured.append)
        ctx = pytypes.SimpleNamespace(
            user_id="u1",
            invocation_id="inv-1",
            function_call_id="fc-1",
            session=pytypes.SimpleNamespace(id="s1"),
        )

        with route_context("feishu", "oc_123"):
            out = spawn_subagent(prompt="summarize logs", tool_context=ctx)

        self.assertEqual(out.get("status"), "pending")
        self.assertTrue(str(out.get("task_id", "")).startswith("subagent-"))
        self.assertEqual(len(captured), 1)
        req = captured[0]
        self.assertEqual(req.user_id, "u1")
        self.assertEqual(req.session_id, "s1")
        self.assertEqual(req.invocation_id, "inv-1")
        self.assertEqual(req.function_call_id, "fc-1")
        self.assertEqual(req.channel, "feishu")
        self.assertEqual(req.chat_id, "oc_123")
        self.assertTrue(req.notify_on_complete)

    def test_spawn_subagent_persists_spawn_record(self) -> None:
        captured: list[SubagentSpawnRequest] = []
        configure_subagent_dispatcher(captured.append)
        ctx = pytypes.SimpleNamespace(
            user_id="u1",
            invocation_id="inv-1",
            function_call_id="fc-1",
            session=pytypes.SimpleNamespace(id="s1"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            with route_context("feishu", "oc_123"):
                out = spawn_subagent(prompt="summarize logs", tool_context=ctx)

            self.assertEqual(out.get("status"), "pending")
            log_path = Path(tmp) / ".openppx" / "subagents.log"
            self.assertTrue(log_path.exists())
            record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["status"], "pending")
            self.assertTrue(str(record["task_id"]).startswith("subagent-"))
            self.assertEqual(record["channel"], "feishu")
            self.assertEqual(record["chat_id"], "oc_123")
            self.assertEqual(record["user_id"], "u1")
            self.assertEqual(record["session_id"], "s1")

    def test_spawn_subagent_records_feedback_event(self) -> None:
        captured: list[SubagentSpawnRequest] = []
        configure_subagent_dispatcher(captured.append)
        ctx = pytypes.SimpleNamespace(
            user_id="u1",
            invocation_id="inv-1",
            function_call_id="fc-1",
            session=pytypes.SimpleNamespace(id="s1"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENPPX_WORKSPACE"] = tmp
            with route_context("feishu", "oc_123"):
                out = spawn_subagent(prompt="summarize logs", tool_context=ctx)

            self.assertEqual(out.get("status"), "pending")
            outbox = Path(tmp) / "messages" / "outbox.log"
            records = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines()]
            feedback = records[-1]
            self.assertEqual(feedback["metadata"]["_feedback_type"], "status")
            self.assertEqual(feedback["metadata"]["_feedback_status"], "accepted")
            self.assertEqual(feedback["metadata"]["_tool_name"], "spawn_subagent")
            self.assertTrue(str(feedback["metadata"]["_task_id"]).startswith("subagent-"))
            self.assertEqual(feedback["metadata"]["_event_class"], "step_update")
            self.assertEqual(feedback["metadata"]["_step_kind"], "subagent")
            self.assertEqual(feedback["metadata"]["_step_phase"], "queued")


if __name__ == "__main__":
    unittest.main()
