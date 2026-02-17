"""Tests for sentientagent_v2 core tools."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from sentientagent_v2.runtime.tool_context import route_context
from sentientagent_v2.tools import (
    cron,
    edit_file,
    exec_command,
    list_dir,
    message,
    message_image,
    read_file,
    web_fetch,
    web_search,
    write_file,
)


class ToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_file_tools_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SENTIENTAGENT_V2_WORKSPACE"] = tmp
            out = write_file("tmp/demo.txt", "hello world")
            self.assertIn("Successfully wrote", out)
            content = read_file("tmp/demo.txt")
            self.assertEqual(content, "hello world")
            edited = edit_file("tmp/demo.txt", "world", "adk")
            self.assertIn("Successfully edited", edited)
            self.assertEqual(read_file("tmp/demo.txt"), "hello adk")

    def test_list_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SENTIENTAGENT_V2_WORKSPACE"] = tmp
            Path(tmp, "a").mkdir()
            Path(tmp, "b.txt").write_text("x", encoding="utf-8")
            listing = list_dir(".")
            self.assertIn("[D] a", listing)
            self.assertIn("[F] b.txt", listing)

    def test_exec_tool(self) -> None:
        result = exec_command("echo hello")
        self.assertIn("hello", result)

    def test_exec_tool_respects_allowlist(self) -> None:
        os.environ["SENTIENTAGENT_V2_EXEC_ALLOWLIST"] = "python"
        out = exec_command("echo hello")
        self.assertIn("allowlist", out.lower())

    def test_exec_tool_is_disabled_when_allow_exec_is_off(self) -> None:
        os.environ["SENTIENTAGENT_V2_ALLOW_EXEC"] = "0"
        out = exec_command("echo hello")
        self.assertIn("disabled by security policy", out.lower())

    def test_file_tools_respect_workspace_restriction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SENTIENTAGENT_V2_WORKSPACE"] = tmp
            os.environ["SENTIENTAGENT_V2_RESTRICT_TO_WORKSPACE"] = "1"
            out = write_file("../outside.txt", "nope")
            self.assertIn("outside workspace", out.lower())

    def test_message_tool_writes_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SENTIENTAGENT_V2_WORKSPACE"] = tmp
            response = message("hi", channel="local", chat_id="u1")
            self.assertIn("Message recorded", response)
            outbox = Path(tmp) / "messages" / "outbox.log"
            self.assertTrue(outbox.exists())

    def test_message_tool_uses_route_context_when_target_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SENTIENTAGENT_V2_WORKSPACE"] = tmp
            with route_context("telegram", "u2"):
                response = message("hi-context", channel=None, chat_id=None)
            self.assertIn("Message recorded", response)
            outbox = Path(tmp) / "messages" / "outbox.log"
            record = json.loads(outbox.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["channel"], "telegram")
            self.assertEqual(record["chat_id"], "u2")

    def test_message_image_tool_writes_image_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SENTIENTAGENT_V2_WORKSPACE"] = tmp
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
            os.environ["SENTIENTAGENT_V2_WORKSPACE"] = tmp
            create = cron(action="add", message="remind me", every_seconds=30)
            self.assertIn("Created job", create)

            listing = cron(action="list")
            self.assertIn("Scheduled jobs", listing)

            job_id = create.split("(id: ", 1)[1].rstrip(")")
            removed = cron(action="remove", job_id=job_id)
            self.assertIn("Removed job", removed)

    def test_web_fetch_rejects_invalid_url(self) -> None:
        payload = json.loads(web_fetch("file:///tmp/test.txt"))
        self.assertIn("error", payload)

    def test_web_tools_respect_security_network_flag(self) -> None:
        os.environ["SENTIENTAGENT_V2_ALLOW_NETWORK"] = "0"
        search_out = web_search("adk")
        fetch_payload = json.loads(web_fetch("https://example.com"))
        self.assertIn("disabled by security policy", search_out.lower())
        self.assertIn("disabled by security policy", fetch_payload["error"].lower())

    def test_web_search_respects_disabled_flag(self) -> None:
        os.environ["SENTIENTAGENT_V2_WEB_ENABLED"] = "0"
        out = web_search("adk")
        self.assertIn("disabled", out.lower())

    def test_web_search_respects_provider_config(self) -> None:
        os.environ["SENTIENTAGENT_V2_WEB_ENABLED"] = "1"
        os.environ["SENTIENTAGENT_V2_WEB_SEARCH_ENABLED"] = "1"
        os.environ["SENTIENTAGENT_V2_WEB_SEARCH_PROVIDER"] = "dummy"
        out = web_search("adk")
        self.assertIn("not supported", out.lower())


if __name__ == "__main__":
    unittest.main()
