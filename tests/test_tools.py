"""Tests for openheron core tools."""

from __future__ import annotations

import json
import os
import tempfile
import types as pytypes
import unittest
from pathlib import Path

from openheron.runtime.tool_context import route_context
from openheron.tools import (
    SubagentSpawnRequest,
    configure_subagent_dispatcher,
    cron,
    edit_file,
    exec_command,
    list_dir,
    message,
    message_image,
    read_file,
    spawn_subagent,
    web_fetch,
    web_search,
    write_file,
)


class ToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        configure_subagent_dispatcher(None)
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_file_tools_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
            out = write_file("tmp/demo.txt", "hello world")
            self.assertIn("Successfully wrote", out)
            content = read_file("tmp/demo.txt")
            self.assertEqual(content, "hello world")
            edited = edit_file("tmp/demo.txt", "world", "adk")
            self.assertIn("Successfully edited", edited)
            self.assertEqual(read_file("tmp/demo.txt"), "hello adk")

    def test_list_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
            Path(tmp, "a").mkdir()
            Path(tmp, "b.txt").write_text("x", encoding="utf-8")
            listing = list_dir(".")
            self.assertIn("[D] a", listing)
            self.assertIn("[F] b.txt", listing)

    def test_exec_tool(self) -> None:
        result = exec_command("echo hello")
        self.assertIn("hello", result)

    def test_exec_tool_supports_shell_compound_command(self) -> None:
        if os.name == "nt":
            cmd = "set OPENHERON_EXEC_TEST=hello && echo %OPENHERON_EXEC_TEST%"
        else:
            cmd = "export OPENHERON_EXEC_TEST=hello && echo $OPENHERON_EXEC_TEST"
        out = exec_command(cmd)
        self.assertIn("hello", out.lower())

    def test_exec_tool_respects_allowlist(self) -> None:
        os.environ["OPENHERON_EXEC_ALLOWLIST"] = "python"
        out = exec_command("echo hello")
        self.assertIn("allowlist", out.lower())

    def test_exec_tool_allowlist_checks_all_chain_segments(self) -> None:
        os.environ["OPENHERON_EXEC_ALLOWLIST"] = "echo"
        out = exec_command("echo ok && python -V")
        self.assertIn("allowlist", out.lower())
        self.assertIn("python", out.lower())

    def test_exec_tool_allowlist_allows_builtin_plus_allowed_command(self) -> None:
        os.environ["OPENHERON_EXEC_ALLOWLIST"] = "echo"
        if os.name == "nt":
            cmd = "set OPENHERON_EXEC_TEST=hello && echo %OPENHERON_EXEC_TEST%"
        else:
            cmd = "export OPENHERON_EXEC_TEST=hello && echo $OPENHERON_EXEC_TEST"
        out = exec_command(cmd)
        self.assertIn("hello", out.lower())

    def test_exec_tool_allowlist_handles_env_assignment_prefix(self) -> None:
        os.environ["OPENHERON_EXEC_ALLOWLIST"] = "echo"
        if os.name == "nt":
            cmd = "set OPENHERON_EXEC_TEST=hello && echo %OPENHERON_EXEC_TEST%"
        else:
            cmd = "OPENHERON_EXEC_TEST=hello echo hello"
        out = exec_command(cmd)
        self.assertIn("hello", out.lower())

    def test_exec_tool_security_mode_deny_blocks_execution(self) -> None:
        os.environ["OPENHERON_EXEC_SECURITY"] = "deny"
        out = exec_command("echo hello")
        self.assertIn("mode=deny", out.lower())

    def test_exec_tool_security_mode_full_ignores_allowlist(self) -> None:
        os.environ["OPENHERON_EXEC_ALLOWLIST"] = "python"
        os.environ["OPENHERON_EXEC_SECURITY"] = "full"
        out = exec_command("echo hello")
        self.assertIn("hello", out.lower())

    def test_exec_tool_allowlist_mode_allows_safe_bins(self) -> None:
        os.environ["OPENHERON_EXEC_ALLOWLIST"] = ""
        os.environ["OPENHERON_EXEC_SECURITY"] = "allowlist"
        os.environ["OPENHERON_EXEC_SAFE_BINS"] = "echo"
        out = exec_command("echo hello")
        self.assertIn("hello", out.lower())

    def test_exec_tool_rejects_invalid_security_mode(self) -> None:
        os.environ["OPENHERON_EXEC_SECURITY"] = "invalid"
        out = exec_command("echo hello")
        self.assertIn("invalid openheron_exec_security", out.lower())

    def test_exec_tool_rejects_invalid_ask_mode(self) -> None:
        os.environ["OPENHERON_EXEC_ASK"] = "invalid"
        out = exec_command("echo hello")
        self.assertIn("invalid openheron_exec_ask", out.lower())

    def test_exec_tool_ask_always_requires_approval(self) -> None:
        os.environ["OPENHERON_EXEC_ASK"] = "always"
        out = exec_command("echo hello")
        self.assertIn("approval required", out.lower())
        self.assertIn("ask=always", out.lower())

    def test_exec_tool_ask_on_miss_requires_approval_for_allowlist_miss(self) -> None:
        os.environ["OPENHERON_EXEC_SECURITY"] = "allowlist"
        os.environ["OPENHERON_EXEC_ALLOWLIST"] = "python"
        os.environ["OPENHERON_EXEC_ASK"] = "on-miss"
        out = exec_command("echo hello")
        self.assertIn("approval required", out.lower())
        self.assertIn("ask=on-miss", out.lower())

    def test_exec_tool_ask_on_miss_allows_allowlist_hit(self) -> None:
        os.environ["OPENHERON_EXEC_SECURITY"] = "allowlist"
        os.environ["OPENHERON_EXEC_ALLOWLIST"] = "echo"
        os.environ["OPENHERON_EXEC_ASK"] = "on-miss"
        out = exec_command("echo hello")
        self.assertIn("hello", out.lower())

    def test_exec_tool_is_disabled_when_allow_exec_is_off(self) -> None:
        os.environ["OPENHERON_ALLOW_EXEC"] = "0"
        out = exec_command("echo hello")
        self.assertIn("disabled by security policy", out.lower())

    def test_file_tools_respect_workspace_restriction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
            os.environ["OPENHERON_RESTRICT_TO_WORKSPACE"] = "1"
            out = write_file("../outside.txt", "nope")
            self.assertIn("outside workspace", out.lower())

    def test_exec_tool_chain_path_guard_blocks_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
            os.environ["OPENHERON_RESTRICT_TO_WORKSPACE"] = "1"
            out = exec_command("echo ok;../outside.sh")
            self.assertIn("outside workspace", out.lower())

    def test_message_tool_writes_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
            response = message("hi", channel="local", chat_id="u1")
            self.assertIn("Message recorded", response)
            outbox = Path(tmp) / "messages" / "outbox.log"
            self.assertTrue(outbox.exists())

    def test_message_tool_uses_route_context_when_target_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
            with route_context("telegram", "u2"):
                response = message("hi-context", channel=None, chat_id=None)
            self.assertIn("Message recorded", response)
            outbox = Path(tmp) / "messages" / "outbox.log"
            record = json.loads(outbox.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["channel"], "telegram")
            self.assertEqual(record["chat_id"], "u2")

    def test_message_image_tool_writes_image_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
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

    def test_cron_tool_add_list_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
            with route_context("telegram", "u2"):
                create = cron(action="add", message="remind me", every_seconds=30)
            self.assertIn("Created job", create)
            store_path = Path(tmp) / ".openheron" / "cron_jobs.json"
            self.assertTrue(store_path.exists())
            payload = json.loads(store_path.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("version"), 2)
            self.assertTrue(payload.get("jobs"))
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

    def test_web_fetch_rejects_invalid_url(self) -> None:
        payload = json.loads(web_fetch("file:///tmp/test.txt"))
        self.assertIn("error", payload)

    def test_web_tools_respect_security_network_flag(self) -> None:
        os.environ["OPENHERON_ALLOW_NETWORK"] = "0"
        search_out = web_search("adk")
        fetch_payload = json.loads(web_fetch("https://example.com"))
        self.assertIn("disabled by security policy", search_out.lower())
        self.assertIn("disabled by security policy", fetch_payload["error"].lower())

    def test_web_search_respects_disabled_flag(self) -> None:
        os.environ["OPENHERON_WEB_ENABLED"] = "0"
        out = web_search("adk")
        self.assertIn("disabled", out.lower())

    def test_web_search_respects_provider_config(self) -> None:
        os.environ["OPENHERON_WEB_ENABLED"] = "1"
        os.environ["OPENHERON_WEB_SEARCH_ENABLED"] = "1"
        os.environ["OPENHERON_WEB_SEARCH_PROVIDER"] = "dummy"
        out = web_search("adk")
        self.assertIn("not supported", out.lower())

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
            os.environ["OPENHERON_WORKSPACE"] = tmp
            with route_context("feishu", "oc_123"):
                out = spawn_subagent(prompt="summarize logs", tool_context=ctx)

            self.assertEqual(out.get("status"), "pending")
            log_path = Path(tmp) / ".openheron" / "subagents.log"
            self.assertTrue(log_path.exists())
            record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["status"], "pending")
            self.assertTrue(str(record["task_id"]).startswith("subagent-"))
            self.assertEqual(record["channel"], "feishu")
            self.assertEqual(record["chat_id"], "oc_123")
            self.assertEqual(record["user_id"], "u1")
            self.assertEqual(record["session_id"], "s1")


if __name__ == "__main__":
    unittest.main()
