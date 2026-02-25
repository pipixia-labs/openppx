"""Tests for openheron core tools."""

from __future__ import annotations

import json
from io import BytesIO
import os
import re
import tempfile
import time
import types as pytypes
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.error import URLError

from openheron.browser_service import BrowserDispatchResponse
from openheron.runtime.tool_context import route_context
from openheron.tools import (
    SubagentSpawnRequest,
    browser,
    configure_browser_runtime,
    configure_subagent_dispatcher,
    cron,
    edit_file,
    exec_command,
    list_dir,
    message,
    message_file,
    message_image,
    process_session,
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
        configure_browser_runtime(None)
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

    def test_read_file_supports_file_path_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
            write_file("tmp/alias.txt", "alias-ok")
            self.assertEqual(read_file(file_path="tmp/alias.txt"), "alias-ok")

    def test_read_file_supports_offset_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
            content = "\n".join(f"line-{idx}" for idx in range(1, 7))
            write_file("tmp/lines.txt", content)

            window = read_file(path="tmp/lines.txt", offset=2, limit=3)
            self.assertIn("line-2\nline-3\nline-4\n", window)
            self.assertIn("[Showing lines 2-4. Use offset=5 to continue.]", window)

            tail = read_file(path="tmp/lines.txt", offset=5)
            self.assertEqual(tail, "line-5\nline-6")

            bad = read_file(path="tmp/lines.txt", offset=0)
            self.assertIn("Error: offset must be a positive integer.", bad)

    def test_read_file_limit_appends_continuation_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
            content = "\n".join(f"line-{idx}" for idx in range(1, 7))
            write_file("tmp/lines.txt", content)

            page = read_file(path="tmp/lines.txt", offset=2, limit=2)
            self.assertIn("line-2", page)
            self.assertIn("line-3", page)
            self.assertIn("[Showing lines 2-3. Use offset=4 to continue.]", page)

    def test_read_file_caps_output_without_explicit_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
            os.environ["OPENHERON_READ_FILE_MAX_BYTES"] = "1024"
            big = "\n".join(f"line-{idx}-{'x' * 80}" for idx in range(1, 400))
            write_file("tmp/big.txt", big)

            output = read_file(path="tmp/big.txt")
            self.assertIn("line-1-", output)
            self.assertIn("[Read output capped at 1KB for this call. Use offset=", output)
            self.assertNotIn("line-399", output)

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

    def test_message_file_tool_writes_file_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENHERON_WORKSPACE"] = tmp
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
            os.environ["OPENHERON_WORKSPACE"] = tmp
            response = message_file("tmp/missing.txt", channel="feishu", chat_id="oc_2")
            self.assertIn("Error: File not found", response)

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
            os.environ["OPENHERON_BROWSER_ARTIFACT_ROOT"] = tmp
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

            os.environ["OPENHERON_BROWSER_ARTIFACT_ROOT"] = tmp
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
            os.environ["OPENHERON_BROWSER_ARTIFACT_ROOT"] = tmp
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
            os.environ["OPENHERON_BROWSER_ARTIFACT_ROOT"] = root_tmp
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
            os.environ["OPENHERON_BROWSER_ARTIFACT_ROOT"] = root_tmp
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
            os.environ["OPENHERON_BROWSER_ARTIFACT_ROOT"] = root_tmp
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
            os.environ["OPENHERON_BROWSER_UPLOAD_ROOT"] = tmp
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

    def test_browser_tool_supports_profiles_and_stop(self) -> None:
        profiles = json.loads(browser(action="profiles"))
        self.assertTrue(profiles["profiles"])
        names = {entry["name"] for entry in profiles["profiles"]}
        self.assertIn("openheron", names)
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
                                "name": "openheron",
                                "attachMode": "launch-or-cdp",
                                "ownershipModel": {"browser": "owned"},
                                "requires": {"OPENHERON_BROWSER_CDP_URL": False},
                                "capability": {
                                    "backend": "playwright",
                                    "attachMode": "launch-or-cdp",
                                    "supportedActions": ["status", "snapshot"],
                                },
                            }
                        ]
                    },
                )

        with patch("openheron.tools.get_browser_control_service", return_value=_FakeBrowserService()):
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
        os.environ["OPENHERON_BROWSER_CONTROL_TOKEN"] = "token-3"
        os.environ["OPENHERON_BROWSER_MUTATION_TOKEN"] = "mut-3"
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_NODE_PROXY_TOKEN"] = "node-token"

        captured: dict[str, str] = {}

        def _fake_urlopen(req, timeout=20):
            captured["url"] = req.full_url
            captured["token"] = req.headers.get("X-openheron-browser-proxy-token", "")
            captured["timeout"] = str(timeout)
            return _DummyResponse('{"ok": true, "via": "node-proxy"}')

        with patch("openheron.tools.urlopen", side_effect=_fake_urlopen):
            payload = json.loads(browser(action="status", target="node", node="node-1", timeout_ms=3500))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["via"], "node-proxy")
        self.assertIn("proxy.local:8787", captured["url"])
        self.assertIn("node=node-1", captured["url"])
        self.assertIn("timeoutMs=3500", captured["url"])
        self.assertEqual(captured["timeout"], "3.5")
        self.assertEqual(captured["token"], "node-token")

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

        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = "http://sandbox-proxy.local:9797"
        os.environ["OPENHERON_BROWSER_PROXY_TOKEN"] = "shared-token"

        captured: dict[str, str] = {}

        def _fake_urlopen(req, timeout=20):
            captured["url"] = req.full_url
            captured["token"] = req.headers.get("X-openheron-browser-proxy-token", "")
            return _DummyResponse('{"ok": true, "via": "sandbox-proxy"}')

        with patch("openheron.tools.urlopen", side_effect=_fake_urlopen):
            payload = json.loads(browser(action="status", target="sandbox"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["via"], "sandbox-proxy")
        self.assertIn("sandbox-proxy.local:9797", captured["url"])
        self.assertEqual(captured["token"], "shared-token")

    def test_browser_tool_blocks_unsupported_action_by_proxy_capability(self) -> None:
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "snapshot"]}}
        )
        with patch("openheron.tools.urlopen") as mocked_urlopen:
            payload = json.loads(browser(action="pdf", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 501)
        self.assertIn("not supported", payload["error"])
        self.assertIn("status", payload["supportedActions"])
        self.assertIn("action=status", payload["hint"])
        mocked_urlopen.assert_not_called()

    def test_browser_tool_unsupported_action_includes_capability_warnings(self) -> None:
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {
                "capability": {
                    "supportedActions": ["status"],
                    "errorCodes": "bad-shape",
                }
            }
        )
        with patch("openheron.tools.urlopen") as mocked_urlopen:
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"supportedActions": ["status", "snapshot"]}
        )
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')) as mocked_urlopen:
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"backend": "node-proxy", "attachMode": "remote", "supportedActions": ["status"]}}
        )
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')):
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"backend": "node-proxy", "supportedActions": ["status"]}}
        )
        with patch(
            "openheron.tools.urlopen",
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')):
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = '{"capability":'
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')):
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"backend": "node-proxy", "supportedActions": ["status"], "errorCodes": "bad-shape"}}
        )
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')):
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = '{"capability":'
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')):
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "snapshot", "tabs"]}}
        )
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')):
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
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
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')):
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_LIMIT"] = "2"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "profiles", "tabs", "snapshot"]}}
        )
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')):
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_LIMIT"] = "bad-value"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "profiles", "tabs", "snapshot", "open", "pdf"]}}
        )
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')):
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_ORDER_JSON"] = json.dumps(
            ["pdf", "snapshot", "status"]
        )
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "profiles", "snapshot", "pdf"]}}
        )
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')):
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_RECOMMENDED_ACTIONS_ORDER_JSON"] = '{"bad":"shape"}'
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"supportedActions": ["status", "profiles", "snapshot"]}}
        )
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')):
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

        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = "http://sandbox-proxy.local:9797"
        with patch("openheron.tools.urlopen", return_value=_DummyResponse('{"ok":true}')):
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
                    body={"ok": False, "error": "profile mismatch: active profile is openheron"},
                )

        with patch("openheron.tools.get_browser_control_service", return_value=_DummyService()):
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

        with patch("openheron.tools.get_browser_control_service", return_value=_DummyService()):
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

        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        with patch("openheron.tools.urlopen", return_value=_DummyResponse()):
            payload = json.loads(browser(action="status", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 502)
        self.assertEqual(payload["errorCode"], "proxy_invalid_json")
        self.assertIn("invalid proxy response", payload["error"])

    def test_browser_tool_uses_structured_proxy_error_payload(self) -> None:
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        http_error = HTTPError(
            url="http://proxy.local:8787/",
            code=502,
            msg="Bad Gateway",
            hdrs=None,
            fp=BytesIO(b'{"error":"rate limited","status":429}'),
        )
        with patch("openheron.tools.urlopen", side_effect=http_error):
            payload = json.loads(browser(action="status", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 429)
        self.assertEqual(payload["error"], "rate limited")
        self.assertEqual(payload["errorCode"], "proxy_http_error")

    def test_browser_tool_maps_proxy_timeout_error(self) -> None:
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        with patch("openheron.tools.urlopen", side_effect=URLError(TimeoutError("timed out"))):
            payload = json.loads(browser(action="status", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 504)
        self.assertEqual(payload["errorCode"], "proxy_timeout")
        self.assertIn("timeout", payload["error"])

    def test_browser_tool_maps_proxy_direct_timeout_error(self) -> None:
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        with patch("openheron.tools.urlopen", side_effect=TimeoutError("timed out")):
            payload = json.loads(browser(action="status", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 504)
        self.assertEqual(payload["errorCode"], "proxy_timeout")

    def test_browser_tool_maps_proxy_connection_refused_error(self) -> None:
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        with patch("openheron.tools.urlopen", side_effect=URLError(ConnectionRefusedError("refused"))):
            payload = json.loads(browser(action="status", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 503)
        self.assertEqual(payload["errorCode"], "proxy_connection_refused")
        self.assertIn("connection refused", payload["error"])

    def test_browser_tool_proxy_http_error_includes_target_capability(self) -> None:
        os.environ["OPENHERON_BROWSER_NODE_PROXY_URL"] = "http://proxy.local:8787"
        os.environ["OPENHERON_BROWSER_NODE_CAPABILITY_JSON"] = json.dumps(
            {"capability": {"backend": "node-proxy", "supportedActions": ["status"]}}
        )
        http_error = HTTPError(
            url="http://proxy.local:8787/",
            code=502,
            msg="Bad Gateway",
            hdrs=None,
            fp=BytesIO(b'{"error":"failed","status":502}'),
        )
        with patch("openheron.tools.urlopen", side_effect=http_error):
            payload = json.loads(browser(action="status", target="node"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["target"], "node")
        self.assertEqual(payload["capability"]["backend"], "node-proxy")

    def test_browser_tool_proxy_url_error_includes_default_target_capability(self) -> None:
        os.environ["OPENHERON_BROWSER_SANDBOX_PROXY_URL"] = "http://sandbox-proxy.local:9797"
        with patch("openheron.tools.urlopen", side_effect=URLError(TimeoutError("timed out"))):
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
        os.environ["OPENHERON_BROWSER_BLOCK_PRIVATE_NETWORKS"] = "0"
        configure_browser_runtime(None)
        json.loads(browser(action="start"))
        opened = json.loads(browser(action="open", target_url="http://127.0.0.1:9222"))
        self.assertTrue(opened["ok"])

    def test_browser_tool_blocks_upload_outside_upload_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            outside_file = Path(outside_tmp) / "upload.txt"
            outside_file.write_text("demo", encoding="utf-8")
            os.environ["OPENHERON_BROWSER_UPLOAD_ROOT"] = root_tmp
            configure_browser_runtime(None)
            json.loads(browser(action="start"))
            json.loads(browser(action="open", target_url="https://example.com"))
            blocked = json.loads(browser(action="upload", paths=[str(outside_file)]))
            self.assertFalse(blocked["ok"])
            self.assertIn("outside upload root", blocked["error"])

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
