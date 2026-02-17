"""Tests for sentientagent_v2 CLI behavior."""

from __future__ import annotations

import json
import os
import tempfile
import types as pytypes
import unittest
from pathlib import Path
from unittest.mock import patch


class CLITests(unittest.TestCase):
    def test_message_mode_dispatch(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_message", return_value=0) as mocked:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["-m", "hello"])
                self.assertEqual(ctx.exception.code, 0)
                mocked.assert_called_once()
                mocked_bootstrap.assert_called_once()

    def test_onboard_mode_dispatch(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_onboard", return_value=0) as mocked_onboard:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["onboard"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_onboard.assert_called_once_with(force=False)
                mocked_bootstrap.assert_not_called()

    def test_doctor_mode_bootstraps_config(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_doctor", return_value=0):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["doctor"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()

    def test_cmd_onboard_creates_config_and_workspace(self) -> None:
        from sentientagent_v2 import cli

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}, clear=False):
                code = cli._cmd_onboard(force=False)

            self.assertEqual(code, 0)
            config_path = Path(tmp) / ".sentientagent_v2" / "config.json"
            self.assertTrue(config_path.exists())
            data = json.loads(config_path.read_text(encoding="utf-8"))
            workspace = Path(data["agent"]["workspace"]).expanduser()
            self.assertTrue(workspace.exists())
            self.assertTrue((workspace / "skills").exists())

    def test_script_entrypoint_accepts_m(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        script_path = project_root / "sentientagent_v2-cli"
        self.assertTrue(script_path.exists())

    def test_cmd_message_collects_final_text(self) -> None:
        from sentientagent_v2 import cli

        fake_event_1 = pytypes.SimpleNamespace(content=pytypes.SimpleNamespace(parts=[]))
        fake_event_2 = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="final answer")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event_1
                yield fake_event_2

        fake_agent = pytypes.SimpleNamespace(name="sentientagent_v2")
        fake_agent_module = pytypes.SimpleNamespace(root_agent=fake_agent)

        with patch.dict("sys.modules", {"sentientagent_v2.agent": fake_agent_module}):
            with patch("sentientagent_v2.cli.create_runner", return_value=(_FakeRunner(), object())):
                with patch("builtins.print") as mocked_print:
                    code = cli._cmd_message("hello", user_id="u1", session_id="s1")

        self.assertEqual(code, 0)
        mocked_print.assert_called_with("final answer")

    def test_cmd_message_merges_stream_snapshots(self) -> None:
        from sentientagent_v2 import cli

        fake_event_1 = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="hello")])
        )
        fake_event_2 = pytypes.SimpleNamespace(
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="hello world")])
        )

        class _FakeRunner:
            async def run_async(self, **kwargs):
                yield fake_event_1
                yield fake_event_2

        fake_agent = pytypes.SimpleNamespace(name="sentientagent_v2")
        fake_agent_module = pytypes.SimpleNamespace(root_agent=fake_agent)

        with patch.dict("sys.modules", {"sentientagent_v2.agent": fake_agent_module}):
            with patch("sentientagent_v2.cli.create_runner", return_value=(_FakeRunner(), object())):
                with patch("builtins.print") as mocked_print:
                    code = cli._cmd_message("hello", user_id="u1", session_id="s1")

        self.assertEqual(code, 0)
        mocked_print.assert_called_with("hello world")


if __name__ == "__main__":
    unittest.main()
