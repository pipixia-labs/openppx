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
        captured: dict[str, object] = {}

        class _FakeRunner:
            async def run_async(self, **kwargs):
                captured.update(kwargs)
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
        request = captured["new_message"]
        text = request.parts[0].text
        self.assertIn("Current request time:", text)
        self.assertIn("Use this as the reference 'now' for relative time expressions", text)
        self.assertIn("\n\nhello", text)

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

    def test_cron_list_mode_dispatch(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_cron_list", return_value=0) as mocked_list:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["cron", "list"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_list.assert_called_once_with(include_disabled=False)
                mocked_bootstrap.assert_called_once()

    def test_cron_add_dispatch_does_not_trigger_single_turn_message(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_message", return_value=0) as mocked_message:
                with patch.object(cli, "_cmd_cron_add", return_value=0) as mocked_add:
                    with self.assertRaises(SystemExit) as ctx:
                        cli.main(
                            [
                                "cron",
                                "add",
                                "--name",
                                "demo",
                                "--message",
                                "hello cron",
                                "--every",
                                "30",
                            ]
                        )
                    self.assertEqual(ctx.exception.code, 0)
                    mocked_add.assert_called_once_with(
                        name="demo",
                        message="hello cron",
                        every=30,
                        cron_expr=None,
                        tz=None,
                        at=None,
                        deliver=False,
                        to=None,
                        channel=None,
                    )
                    mocked_message.assert_not_called()
                    mocked_bootstrap.assert_called_once()

    def test_cmd_cron_add_validates_deliver_target(self) -> None:
        from sentientagent_v2 import cli

        with patch("builtins.print") as mocked_print:
            code = cli._cmd_cron_add(
                name="demo",
                message="hello",
                every=30,
                cron_expr=None,
                tz=None,
                at=None,
                deliver=True,
                to=None,
                channel=None,
            )
        self.assertEqual(code, 1)
        mocked_print.assert_called_with("Error: --to is required when --deliver is set")

    def test_cmd_cron_add_persists_job(self) -> None:
        from sentientagent_v2 import cli

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"SENTIENTAGENT_V2_WORKSPACE": tmp}, clear=False):
                with patch("builtins.print") as mocked_print:
                    code = cli._cmd_cron_add(
                        name="demo",
                        message="hello cron",
                        every=30,
                        cron_expr=None,
                        tz=None,
                        at=None,
                        deliver=False,
                        to=None,
                        channel=None,
                    )
            self.assertEqual(code, 0)
            out = mocked_print.call_args[0][0]
            self.assertIn("Added job 'demo'", out)
            store = Path(tmp) / ".sentientagent_v2" / "cron_jobs.json"
            self.assertTrue(store.exists())

    def test_cmd_cron_run_reports_no_callback_in_plain_cli_process(self) -> None:
        from sentientagent_v2 import cli

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"SENTIENTAGENT_V2_WORKSPACE": tmp}, clear=False):
                add_code = cli._cmd_cron_add(
                    name="demo",
                    message="hello cron",
                    every=30,
                    cron_expr=None,
                    tz=None,
                    at=None,
                    deliver=False,
                    to=None,
                    channel=None,
                )
                self.assertEqual(add_code, 0)
                job_id = cli._cron_service().list_jobs(include_disabled=True)[0].id
                with patch("builtins.print") as mocked_print:
                    code = cli._cmd_cron_run(job_id, force=False)

        self.assertEqual(code, 1)
        self.assertIn("no executor callback", mocked_print.call_args[0][0])

    def test_cmd_cron_status_prints_runtime_fields(self) -> None:
        from sentientagent_v2 import cli

        fake_service = pytypes.SimpleNamespace(
            status=lambda: {
                "running": False,
                "runtime_active": True,
                "runtime_pid": 12345,
                "runtime_last_seen_at_ms": 1739877000000,
                "jobs": 2,
                "next_wake_at_ms": None,
            }
        )
        with patch.object(cli, "_cron_service", return_value=fake_service):
            with patch("builtins.print") as mocked_print:
                code = cli._cmd_cron_status()

        self.assertEqual(code, 0)
        line = mocked_print.call_args[0][0]
        self.assertIn("local_running=False", line)
        self.assertIn("runtime_active=True", line)
        self.assertIn("runtime_pid=12345", line)


if __name__ == "__main__":
    unittest.main()
