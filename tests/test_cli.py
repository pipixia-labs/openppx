"""Tests for sentientagent_v2 CLI behavior."""

from __future__ import annotations

import json
import os
import tempfile
import types as pytypes
import unittest
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


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

    def test_doctor_mode_passes_json_and_verbose_flags(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli, "bootstrap_env_from_config"):
            with patch.object(cli, "_cmd_doctor", return_value=0) as mocked_doctor:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["doctor", "--json", "--verbose"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_doctor.assert_called_once_with(output_json=True, verbose=True)

    def test_provider_login_mode_dispatch(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_provider_login", return_value=0) as mocked_login:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["provider", "login", "openai-codex"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()
                mocked_login.assert_called_once_with("openai-codex")

    def test_provider_list_mode_dispatch(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_provider_list", return_value=0) as mocked_list:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["provider", "list"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()
                mocked_list.assert_called_once_with()

    def test_provider_status_mode_dispatch(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_provider_status", return_value=0) as mocked_status:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["provider", "status", "--json"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()
                mocked_status.assert_called_once_with(output_json=True)

    def test_cmd_provider_login_rejects_non_oauth_provider(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli.logger, "info") as mocked_info:
            code = cli._cmd_provider_login("openai")
        self.assertEqual(code, 1)
        self.assertIn("Unknown OAuth provider", mocked_info.call_args[0][0])

    def test_cmd_provider_login_invokes_registered_handler(self) -> None:
        from sentientagent_v2 import cli

        handler = Mock()
        with patch.dict(cli._PROVIDER_LOGIN_HANDLERS, {"openai_codex": handler}, clear=False):
            with patch.object(cli.logger, "info"):
                code = cli._cmd_provider_login("openai-codex")
        self.assertEqual(code, 0)
        handler.assert_called_once_with()

    def test_cmd_provider_login_openai_codex_uses_cached_valid_token(self) -> None:
        from sentientagent_v2 import cli

        token = pytypes.SimpleNamespace(access="token", account_id="acct_1")
        fake_oauth_module = pytypes.SimpleNamespace(
            get_token=Mock(return_value=token),
            login_oauth_interactive=Mock(return_value=token),
        )
        with patch.dict(sys.modules, {"oauth_cli_kit": fake_oauth_module}):
            with patch.object(cli.logger, "info"):
                code = cli._cmd_provider_login("openai-codex")
        self.assertEqual(code, 0)
        fake_oauth_module.login_oauth_interactive.assert_not_called()

    def test_cmd_provider_login_openai_codex_rejects_missing_account_id(self) -> None:
        from sentientagent_v2 import cli

        token = pytypes.SimpleNamespace(access="token", account_id="")
        fake_oauth_module = pytypes.SimpleNamespace(
            get_token=Mock(return_value=token),
            login_oauth_interactive=Mock(return_value=token),
        )
        with patch.dict(sys.modules, {"oauth_cli_kit": fake_oauth_module}):
            with patch.object(cli.logger, "info") as mocked_info:
                code = cli._cmd_provider_login("openai-codex")

        self.assertEqual(code, 1)
        fake_oauth_module.login_oauth_interactive.assert_called_once()
        lines = [call.args[0] for call in mocked_info.call_args_list if call.args]
        self.assertTrue(any("account_id missing in token" in line for line in lines))

    def test_provider_oauth_health_non_oauth_provider(self) -> None:
        from sentientagent_v2 import cli

        issue, status = cli._provider_oauth_health("google")
        self.assertIsNone(issue)
        self.assertFalse(status["required"])
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["message"], "not_required")

    def test_provider_oauth_health_openai_codex_missing_token(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli, "_check_openai_codex_oauth", return_value=(False, "token missing")):
            issue, status = cli._provider_oauth_health("openai_codex")
        self.assertIsNotNone(issue)
        self.assertIn("provider login openai-codex", str(issue))
        self.assertTrue(status["required"])
        self.assertFalse(status["authenticated"])
        self.assertEqual(status["message"], "token missing")

    def test_provider_oauth_health_openai_codex_authenticated(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli, "_check_openai_codex_oauth", return_value=(True, "account_id=user_1")):
            issue, status = cli._provider_oauth_health("openai_codex")
        self.assertIsNone(issue)
        self.assertTrue(status["required"])
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["message"], "account_id=user_1")

    def test_cmd_doctor_includes_mcp_health_failures(self) -> None:
        from sentientagent_v2 import cli

        fake_registry = pytypes.SimpleNamespace(workspace=Path("/tmp"), list_skills=lambda: [])
        fake_session_cfg = pytypes.SimpleNamespace(db_url="sqlite+aiosqlite:////tmp/sessions.db")
        fake_security_policy = pytypes.SimpleNamespace(
            restrict_to_workspace=False,
            allow_exec=True,
            allow_network=True,
            exec_allowlist=(),
        )
        fake_mcp_result = {
            "name": "filesystem",
            "transport": "stdio",
            "prefix": "mcp_filesystem_",
            "status": "error",
            "tool_count": 0,
            "elapsed_ms": 3,
            "error": "boom",
        }
        with patch.dict(
            os.environ,
            {
                "SENTIENTAGENT_V2_PROVIDER": "google",
                "SENTIENTAGENT_V2_PROVIDER_ENABLED": "1",
                "GOOGLE_API_KEY": "k",
            },
            clear=False,
        ):
            with patch("sentientagent_v2.cli.shutil.which", return_value="/usr/bin/adk"):
                with patch.object(cli, "validate_provider_runtime", return_value=None):
                    with patch.object(cli, "get_registry", return_value=fake_registry):
                        with patch.object(cli, "load_session_config", return_value=fake_session_cfg):
                            with patch.object(cli, "parse_enabled_channels", return_value=["local"]):
                                with patch.object(cli, "validate_channel_setup", return_value=[]):
                                    with patch.object(cli, "load_security_policy", return_value=fake_security_policy):
                                        with patch.object(cli, "build_mcp_toolsets_from_env", return_value=[object()]):
                                            with patch.object(
                                                cli,
                                                "summarize_mcp_toolsets",
                                                return_value=[{"name": "filesystem", "transport": "stdio", "prefix": "mcp_filesystem_"}],
                                            ):
                                                with patch.object(
                                                    cli,
                                                    "probe_mcp_toolsets",
                                                    new=AsyncMock(return_value=[fake_mcp_result]),
                                                ):
                                                    with patch.object(cli.logger, "debug"):
                                                        with patch.object(cli.logger, "info") as mocked_info:
                                                            code = cli._cmd_doctor(output_json=False, verbose=False)

        self.assertEqual(code, 1)
        info_text = "\n".join(call.args[0] for call in mocked_info.call_args_list if call.args)
        self.assertIn("Issues:", info_text)
        self.assertIn("MCP server 'filesystem' health check failed", info_text)

    def test_cmd_doctor_json_output_for_automation(self) -> None:
        from sentientagent_v2 import cli

        fake_registry = pytypes.SimpleNamespace(workspace=Path("/tmp"), list_skills=lambda: [])
        fake_session_cfg = pytypes.SimpleNamespace(db_url="sqlite+aiosqlite:////tmp/sessions.db")
        fake_security_policy = pytypes.SimpleNamespace(
            restrict_to_workspace=False,
            allow_exec=True,
            allow_network=True,
            exec_allowlist=(),
        )
        with patch.dict(
            os.environ,
            {
                "SENTIENTAGENT_V2_PROVIDER": "google",
                "SENTIENTAGENT_V2_PROVIDER_ENABLED": "1",
                "GOOGLE_API_KEY": "k",
            },
            clear=False,
        ):
            with patch("sentientagent_v2.cli.shutil.which", return_value="/usr/bin/adk"):
                with patch.object(cli, "validate_provider_runtime", return_value=None):
                    with patch.object(cli, "get_registry", return_value=fake_registry):
                        with patch.object(cli, "load_session_config", return_value=fake_session_cfg):
                            with patch.object(cli, "parse_enabled_channels", return_value=["local"]):
                                with patch.object(cli, "validate_channel_setup", return_value=[]):
                                    with patch.object(cli, "load_security_policy", return_value=fake_security_policy):
                                        with patch.object(cli, "build_mcp_toolsets_from_env", return_value=[]):
                                            with patch.object(cli.logger, "debug"):
                                                with patch("builtins.print") as mocked_print:
                                                    code = cli._cmd_doctor(output_json=True, verbose=False)
        self.assertEqual(code, 0)
        self.assertEqual(mocked_print.call_count, 1)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertTrue(payload["ok"])
        self.assertIn("mcp", payload)
        self.assertIn("issues", payload)

    def test_cmd_doctor_json_output_includes_provider_oauth_issue(self) -> None:
        from sentientagent_v2 import cli

        fake_registry = pytypes.SimpleNamespace(workspace=Path("/tmp"), list_skills=lambda: [])
        fake_session_cfg = pytypes.SimpleNamespace(db_url="sqlite+aiosqlite:////tmp/sessions.db")
        fake_security_policy = pytypes.SimpleNamespace(
            restrict_to_workspace=False,
            allow_exec=True,
            allow_network=True,
            exec_allowlist=(),
        )
        fake_oauth_status = {
            "required": True,
            "authenticated": False,
            "message": "token missing",
        }
        with patch.dict(
            os.environ,
            {
                "SENTIENTAGENT_V2_PROVIDER": "openai_codex",
                "SENTIENTAGENT_V2_PROVIDER_ENABLED": "1",
            },
            clear=False,
        ):
            with patch("sentientagent_v2.cli.shutil.which", return_value="/usr/bin/adk"):
                with patch.object(cli, "validate_provider_runtime", return_value=None):
                    with patch.object(
                        cli,
                        "_provider_oauth_health",
                        return_value=("OpenAI Codex OAuth token is not ready", fake_oauth_status),
                    ):
                        with patch.object(cli, "get_registry", return_value=fake_registry):
                            with patch.object(cli, "load_session_config", return_value=fake_session_cfg):
                                with patch.object(cli, "parse_enabled_channels", return_value=["local"]):
                                    with patch.object(cli, "validate_channel_setup", return_value=[]):
                                        with patch.object(cli, "load_security_policy", return_value=fake_security_policy):
                                            with patch.object(cli, "build_mcp_toolsets_from_env", return_value=[]):
                                                with patch.object(cli.logger, "debug"):
                                                    with patch("builtins.print") as mocked_print:
                                                        code = cli._cmd_doctor(output_json=True, verbose=False)

        self.assertEqual(code, 1)
        self.assertEqual(mocked_print.call_count, 1)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertFalse(payload["ok"])
        self.assertIn("OpenAI Codex OAuth token is not ready", payload["issues"])
        self.assertEqual(payload["provider"]["oauth"], fake_oauth_status)

    def test_cmd_provider_status_json_output_includes_oauth_issue(self) -> None:
        from sentientagent_v2 import cli

        fake_oauth_status = {
            "required": True,
            "authenticated": False,
            "message": "token missing",
        }
        with patch.dict(
            os.environ,
            {
                "SENTIENTAGENT_V2_PROVIDER": "openai_codex",
                "SENTIENTAGENT_V2_PROVIDER_ENABLED": "1",
            },
            clear=False,
        ):
            with patch.object(cli, "validate_provider_runtime", return_value=None):
                with patch.object(
                    cli,
                    "_provider_oauth_health",
                    return_value=("OpenAI Codex OAuth token is not ready", fake_oauth_status),
                ):
                    with patch("builtins.print") as mocked_print:
                        code = cli._cmd_provider_status(output_json=True)

        self.assertEqual(code, 1)
        self.assertEqual(mocked_print.call_count, 1)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertFalse(payload["ok"])
        self.assertIn("OpenAI Codex OAuth token is not ready", payload["issues"])
        self.assertEqual(payload["provider"]["oauth"], fake_oauth_status)

    def test_log_mcp_startup_summary(self) -> None:
        from sentientagent_v2 import cli

        with patch.object(cli, "summarize_mcp_toolsets", return_value=[]):
            with patch.object(cli.logger, "info") as mocked_info:
                cli._log_mcp_startup_summary([])
        mocked_info.assert_called_once_with("MCP toolsets: none configured")

        with patch.object(
            cli,
            "summarize_mcp_toolsets",
            return_value=[{"name": "filesystem", "transport": "stdio", "prefix": "mcp_filesystem_"}],
        ):
            with patch.object(cli.logger, "info") as mocked_info:
                cli._log_mcp_startup_summary([object()])
        self.assertEqual(mocked_info.call_count, 2)
        self.assertEqual(mocked_info.call_args_list[0].args[0], "MCP toolsets: 1 server(s) configured")
        self.assertIn("MCP server filesystem", mocked_info.call_args_list[1].args[0])

    def test_required_mcp_preflight_fails_when_required_server_missing(self) -> None:
        from sentientagent_v2 import cli

        with patch.dict(
            os.environ,
            {"SENTIENTAGENT_V2_MCP_REQUIRED_SERVERS": "filesystem,docs"},
            clear=False,
        ):
            issues = asyncio.run(cli._required_mcp_preflight([]))
        self.assertTrue(issues)
        self.assertIn("missing from configured toolsets", issues[0])

    def test_required_mcp_preflight_fails_when_required_server_unhealthy(self) -> None:
        from sentientagent_v2 import cli
        from sentientagent_v2.mcp_registry import build_mcp_toolsets

        toolsets = build_mcp_toolsets(
            {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}},
            log_registered=False,
        )
        fake_result = {
            "name": "filesystem",
            "transport": "stdio",
            "prefix": "mcp_filesystem_",
            "status": "error",
            "error": "boom",
            "error_kind": "config",
            "tool_count": 0,
            "elapsed_ms": 12,
            "attempts": 1,
        }
        with patch.dict(
            os.environ,
            {"SENTIENTAGENT_V2_MCP_REQUIRED_SERVERS": "filesystem"},
            clear=False,
        ):
            with patch.object(cli, "probe_mcp_toolsets", new=AsyncMock(return_value=[fake_result])):
                issues = asyncio.run(cli._required_mcp_preflight(toolsets))
        self.assertTrue(issues)
        self.assertIn("required MCP server 'filesystem' failed", issues[0])

    def test_cmd_gateway_exits_when_required_mcp_preflight_fails(self) -> None:
        from sentientagent_v2 import cli

        fake_agent = pytypes.SimpleNamespace(name="sentientagent_v2", tools=[])
        fake_agent_module = pytypes.SimpleNamespace(root_agent=fake_agent)

        class _UnexpectedGateway:
            def __init__(self, *args, **kwargs):
                raise AssertionError("Gateway should not be constructed when MCP preflight fails")

        fake_gateway_module = pytypes.SimpleNamespace(Gateway=_UnexpectedGateway)

        with patch.dict(
            sys.modules,
            {
                "sentientagent_v2.agent": fake_agent_module,
                "sentientagent_v2.gateway": fake_gateway_module,
            },
        ):
            with patch.object(cli, "parse_enabled_channels", return_value=["local"]):
                with patch.object(cli, "validate_channel_setup", return_value=[]):
                    with patch.object(
                        cli,
                        "_required_mcp_preflight",
                        new=AsyncMock(return_value=["required MCP failed"]),
                    ):
                        with patch.object(cli.logger, "info") as mocked_info:
                            code = cli._cmd_gateway(
                                channels="local",
                                sender_id="u1",
                                chat_id="c1",
                                interactive_local=False,
                            )
        self.assertEqual(code, 1)
        messages = [call.args[0] for call in mocked_info.call_args_list if call.args]
        self.assertIn("[doctor] required MCP failed", messages)

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
                with patch.object(cli.logger, "info") as mocked_info:
                    code = cli._cmd_message("hello", user_id="u1", session_id="s1")

        self.assertEqual(code, 0)
        mocked_info.assert_called_with("final answer")
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
                with patch.object(cli.logger, "info") as mocked_info:
                    code = cli._cmd_message("hello", user_id="u1", session_id="s1")

        self.assertEqual(code, 0)
        mocked_info.assert_called_with("hello world")

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

        with patch.object(cli.logger, "info") as mocked_info:
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
        mocked_info.assert_called_with("Error: --to is required when --deliver is set")

    def test_cmd_cron_add_persists_job(self) -> None:
        from sentientagent_v2 import cli

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"SENTIENTAGENT_V2_WORKSPACE": tmp}, clear=False):
                with patch.object(cli.logger, "info") as mocked_info:
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
            out = mocked_info.call_args[0][0]
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
                with patch.object(cli.logger, "info") as mocked_info:
                    code = cli._cmd_cron_run(job_id, force=False)

        self.assertEqual(code, 1)
        self.assertIn("no executor callback", mocked_info.call_args[0][0])

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
            with patch.object(cli.logger, "info") as mocked_info:
                code = cli._cmd_cron_status()

        self.assertEqual(code, 0)
        line = mocked_info.call_args[0][0]
        self.assertIn("local_running=False", line)
        self.assertIn("runtime_active=True", line)
        self.assertIn("runtime_pid=12345", line)

    def test_cmd_cron_list_uses_plain_stdout(self) -> None:
        from sentientagent_v2 import cli

        fake_schedule = pytypes.SimpleNamespace(kind="every", every_seconds=30)
        fake_state = pytypes.SimpleNamespace(next_run_at_ms=None)
        fake_job = pytypes.SimpleNamespace(id="j1", name="demo", enabled=True, schedule=fake_schedule, state=fake_state)
        fake_service = pytypes.SimpleNamespace(list_jobs=lambda include_disabled: [fake_job])

        with patch.object(cli, "_cron_service", return_value=fake_service):
            with patch("builtins.print") as mocked_print:
                with patch.object(cli.logger, "info") as mocked_info:
                    code = cli._cmd_cron_list(include_disabled=True)

        self.assertEqual(code, 0)
        self.assertEqual(mocked_print.call_count, 2)
        mocked_print.assert_any_call("Scheduled jobs:")
        mocked_print.assert_any_call("- demo (id: j1, every:30s, enabled, next=-)")
        mocked_info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
