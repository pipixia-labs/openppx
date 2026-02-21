"""Tests for openheron CLI behavior."""

from __future__ import annotations

import json
import os
import tempfile
import types as pytypes
import unittest
import asyncio
import sys
import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


class CLITests(unittest.TestCase):
    def test_message_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_message", return_value=0) as mocked:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["-m", "hello"])
                self.assertEqual(ctx.exception.code, 0)
                mocked.assert_called_once()
                mocked_bootstrap.assert_called_once()

    def test_onboard_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_onboard", return_value=0) as mocked_onboard:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["onboard"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_onboard.assert_called_once_with(force=False)
                mocked_bootstrap.assert_not_called()

    def test_doctor_mode_bootstraps_config(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_doctor", return_value=0):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["doctor"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()

    def test_doctor_mode_passes_json_and_verbose_flags(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config"):
            with patch.object(cli, "_cmd_doctor", return_value=0) as mocked_doctor:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["doctor", "--json", "--verbose"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_doctor.assert_called_once_with(output_json=True, verbose=True)

    def test_mcps_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_mcps", return_value=0) as mocked_mcps:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["mcps"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()
                mocked_mcps.assert_called_once_with()

    def test_spawn_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_spawn", return_value=0) as mocked_spawn:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["spawn"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()
                mocked_spawn.assert_called_once_with()

    def test_provider_login_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_provider_login", return_value=0) as mocked_login:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["provider", "login", "openai-codex"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()
                mocked_login.assert_called_once_with("openai-codex")

    def test_provider_list_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_provider_list", return_value=0) as mocked_list:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["provider", "list"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()
                mocked_list.assert_called_once_with()

    def test_cmd_provider_list_includes_runtime_and_default_model(self) -> None:
        from openheron import cli

        with patch.object(cli.logger, "info") as mocked_info:
            code = cli._cmd_provider_list()
        self.assertEqual(code, 0)
        lines = [call.args[0] for call in mocked_info.call_args_list if call.args]
        self.assertTrue(any("openai_codex: runtime=codex" in line for line in lines))
        self.assertTrue(any("default_model=" in line for line in lines))

    def test_provider_status_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_provider_status", return_value=0) as mocked_status:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["provider", "status", "--json"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()
                mocked_status.assert_called_once_with(output_json=True)

    def test_channels_login_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_channels_login", return_value=0) as mocked_login:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["channels", "login"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()
                mocked_login.assert_called_once_with(channel_name="whatsapp")

    def test_channels_bridge_start_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_channels_bridge_start", return_value=0) as mocked_start:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["channels", "bridge", "start"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()
                mocked_start.assert_called_once_with(channel_name="whatsapp")

    def test_cmd_provider_login_rejects_non_oauth_provider(self) -> None:
        from openheron import cli

        with patch.object(cli.logger, "info") as mocked_info:
            code = cli._cmd_provider_login("openai")
        self.assertEqual(code, 1)
        self.assertIn("Unknown OAuth provider", mocked_info.call_args[0][0])

    def test_cmd_provider_login_invokes_registered_handler(self) -> None:
        from openheron import cli

        handler = Mock()
        with patch.dict(cli._PROVIDER_LOGIN_HANDLERS, {"openai_codex": handler}, clear=False):
            with patch.object(cli.logger, "info"):
                code = cli._cmd_provider_login("openai-codex")
        self.assertEqual(code, 0)
        handler.assert_called_once_with()

    def test_cmd_provider_login_accepts_alias(self) -> None:
        from openheron import cli

        handler = Mock()
        with patch.dict(cli._PROVIDER_LOGIN_HANDLERS, {"openai_codex": handler}, clear=False):
            with patch.object(cli.logger, "info"):
                code = cli._cmd_provider_login("codex")
        self.assertEqual(code, 0)
        handler.assert_called_once_with()

    def test_cmd_provider_login_openai_codex_uses_cached_valid_token(self) -> None:
        from openheron import cli

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
        from openheron import cli

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
        from openheron import cli

        issue, status = cli._provider_oauth_health("google")
        self.assertIsNone(issue)
        self.assertFalse(status["required"])
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["message"], "not_required")

    def test_provider_oauth_health_openai_codex_missing_token(self) -> None:
        from openheron import cli

        with patch.object(cli, "_check_openai_codex_oauth", return_value=(False, "token missing")):
            issue, status = cli._provider_oauth_health("openai_codex")
        self.assertIsNotNone(issue)
        self.assertIn("provider login openai-codex", str(issue))
        self.assertTrue(status["required"])
        self.assertFalse(status["authenticated"])
        self.assertEqual(status["message"], "token missing")

    def test_provider_oauth_health_openai_codex_authenticated(self) -> None:
        from openheron import cli

        with patch.object(cli, "_check_openai_codex_oauth", return_value=(True, "account_id=user_1")):
            issue, status = cli._provider_oauth_health("openai_codex")
        self.assertIsNone(issue)
        self.assertTrue(status["required"])
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["message"], "account_id=user_1")

    def test_check_github_copilot_oauth_non_invasive_missing_cache(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"GITHUB_COPILOT_TOKEN_DIR": tmp}, clear=False):
                ok, detail = cli._check_github_copilot_oauth_non_invasive()
        self.assertFalse(ok)
        self.assertEqual(detail, "access_token_missing")

    def test_check_github_copilot_oauth_non_invasive_valid_api_key_cache(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            cache = {
                "token": "ghu_xxx",
                "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).timestamp(),
            }
            api_key_path = Path(tmp) / "api-key.json"
            api_key_path.write_text(json.dumps(cache), encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_COPILOT_TOKEN_DIR": tmp}, clear=False):
                ok, detail = cli._check_github_copilot_oauth_non_invasive()
        self.assertTrue(ok)
        self.assertIn("api_key_cached_until=", detail)

    def test_provider_oauth_health_github_copilot_missing_cache_returns_issue(self) -> None:
        from openheron import cli

        with patch.object(cli, "_check_github_copilot_oauth_non_invasive", return_value=(False, "access_token_missing")):
            issue, status = cli._provider_oauth_health("github_copilot")
        self.assertIsNotNone(issue)
        self.assertIn("provider login github-copilot", str(issue))
        self.assertTrue(status["required"])
        self.assertFalse(status["authenticated"])
        self.assertEqual(status["message"], "access_token_missing")

    def test_cmd_doctor_includes_mcp_health_failures(self) -> None:
        from openheron import cli

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
                "OPENHERON_PROVIDER": "google",
                "OPENHERON_PROVIDER_ENABLED": "1",
                "GOOGLE_API_KEY": "k",
            },
            clear=False,
        ):
            with patch("openheron.cli.shutil.which", return_value="/usr/bin/adk"):
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
        from openheron import cli

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
                "OPENHERON_PROVIDER": "google",
                "OPENHERON_PROVIDER_ENABLED": "1",
                "GOOGLE_API_KEY": "k",
            },
            clear=False,
        ):
            with patch("openheron.cli.shutil.which", return_value="/usr/bin/adk"):
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
        from openheron import cli

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
                "OPENHERON_PROVIDER": "openai_codex",
                "OPENHERON_PROVIDER_ENABLED": "1",
            },
            clear=False,
        ):
            with patch("openheron.cli.shutil.which", return_value="/usr/bin/adk"):
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
        from openheron import cli

        fake_oauth_status = {
            "required": True,
            "authenticated": False,
            "message": "token missing",
        }
        with patch.dict(
            os.environ,
            {
                "OPENHERON_PROVIDER": "openai_codex",
                "OPENHERON_PROVIDER_ENABLED": "1",
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
        from openheron import cli

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

    def test_cmd_mcps_lists_connected_servers_and_api_names(self) -> None:
        from openheron import cli

        fake_toolset_ok = pytypes.SimpleNamespace(meta=pytypes.SimpleNamespace(name="filesystem"))
        fake_toolset_bad = pytypes.SimpleNamespace(meta=pytypes.SimpleNamespace(name="bad_remote"))
        fake_results = [
            {"name": "filesystem", "transport": "stdio", "status": "ok"},
            {"name": "bad_remote", "transport": "http", "status": "error"},
        ]

        with patch.object(cli, "build_mcp_toolsets_from_env", return_value=[fake_toolset_ok, fake_toolset_bad]):
            with patch.object(cli, "probe_mcp_toolsets", new=AsyncMock(return_value=fake_results)):
                with patch.object(
                    cli,
                    "_collect_connected_mcp_apis",
                    new=AsyncMock(
                        return_value={
                            "filesystem": [
                                {
                                    "name": "read_file",
                                    "description": "Read file content",
                                    "input": "fields=path(required)",
                                    "output": "type=string",
                                },
                                {
                                    "name": "write_file",
                                    "description": "Write file content",
                                    "input": "fields=path(required),content(required)",
                                    "output": "type=object",
                                },
                            ]
                        }
                    ),
                ):
                    with patch.object(cli.logger, "info") as mocked_info:
                        code = cli._cmd_mcps()

        self.assertEqual(code, 0)
        info_text = "\n".join(call.args[0] for call in mocked_info.call_args_list if call.args)
        self.assertIn("Connected MCP servers: 1", info_text)
        self.assertIn("filesystem (stdio)", info_text)
        self.assertIn("APIs (2):", info_text)
        self.assertIn("read_file", info_text)
        self.assertIn("功能: Read file content", info_text)
        self.assertIn("输入: fields=path(required)", info_text)
        self.assertIn("输出: type=string", info_text)
        self.assertNotIn("bad_remote (http)", info_text)

    def test_collect_connected_mcp_apis_extracts_input_output_and_description(self) -> None:
        from openheron import cli

        fake_raw_tool = pytypes.SimpleNamespace(
            description="Read file content",
            inputSchema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            outputSchema={"type": "string"},
        )
        fake_tool = pytypes.SimpleNamespace(name="read_file", raw_mcp_tool=fake_raw_tool)
        fake_toolset = pytypes.SimpleNamespace(meta=pytypes.SimpleNamespace(name="filesystem"))

        with patch.object(cli.ManagedMcpToolset, "get_tools", new=AsyncMock(return_value=[fake_tool])):
            rows_by_server = asyncio.run(cli._collect_connected_mcp_apis([fake_toolset], timeout_seconds=3.0))

        rows = rows_by_server["filesystem"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "read_file")
        self.assertEqual(rows[0]["description"], "Read file content")
        self.assertIn("path(required)", rows[0]["input"])
        self.assertEqual(rows[0]["output"], "type=string")

    def test_cmd_spawn_lists_recent_subagent_records(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / ".openheron"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "subagents.log"
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-02-21T10:00:00",
                                "status": "pending",
                                "task_id": "subagent-111",
                                "prompt_preview": "first",
                                "channel": "feishu",
                                "chat_id": "oc_1",
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-02-21T10:01:00",
                                "status": "pending",
                                "task_id": "subagent-222",
                                "prompt_preview": "second",
                                "channel": "local",
                                "chat_id": "terminal",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            fake_policy = pytypes.SimpleNamespace(workspace_root=Path(tmp))
            with patch.object(cli, "load_security_policy", return_value=fake_policy):
                with patch.object(cli.logger, "info") as mocked_info:
                    code = cli._cmd_spawn()

        self.assertEqual(code, 0)
        info_text = "\n".join(call.args[0] for call in mocked_info.call_args_list if call.args)
        self.assertIn("Subagents: 2 recent task(s)", info_text)
        self.assertIn("subagent-222", info_text)
        self.assertIn("subagent-111", info_text)
        self.assertIn("prompt: second", info_text)

    def test_required_mcp_preflight_fails_when_required_server_missing(self) -> None:
        from openheron import cli

        with patch.dict(
            os.environ,
            {"OPENHERON_MCP_REQUIRED_SERVERS": "filesystem,docs"},
            clear=False,
        ):
            issues = asyncio.run(cli._required_mcp_preflight([]))
        self.assertTrue(issues)
        self.assertIn("missing from configured toolsets", issues[0])

    def test_required_mcp_preflight_fails_when_required_server_unhealthy(self) -> None:
        from openheron import cli
        from openheron.mcp_registry import build_mcp_toolsets

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
            {"OPENHERON_MCP_REQUIRED_SERVERS": "filesystem"},
            clear=False,
        ):
            with patch.object(cli, "probe_mcp_toolsets", new=AsyncMock(return_value=[fake_result])):
                issues = asyncio.run(cli._required_mcp_preflight(toolsets))
        self.assertTrue(issues)
        self.assertIn("required MCP server 'filesystem' failed", issues[0])

    def test_cmd_gateway_continues_when_required_mcp_preflight_health_check_fails(self) -> None:
        from openheron import cli

        fake_agent = pytypes.SimpleNamespace(name="openheron", tools=[])
        fake_agent_module = pytypes.SimpleNamespace(root_agent=fake_agent)
        state: dict[str, bool] = {"constructed": False, "started": False, "stopped": False}

        class _FakeGateway:
            def __init__(self, *args, **kwargs):
                state["constructed"] = True

            async def start(self):
                state["started"] = True

            async def stop(self):
                state["stopped"] = True

        fake_gateway_module = pytypes.SimpleNamespace(Gateway=_FakeGateway)

        with patch.dict(
            sys.modules,
            {
                "openheron.agent": fake_agent_module,
                "openheron.gateway": fake_gateway_module,
            },
        ):
            with patch.object(cli, "parse_enabled_channels", return_value=["local"]):
                with patch.object(cli, "validate_channel_setup", return_value=[]):
                    with patch.object(
                        cli,
                        "_required_mcp_preflight",
                        new=AsyncMock(return_value=["required MCP failed health check (error/transient)"]),
                    ):
                        with patch.object(cli.logger, "warning") as mocked_warning:
                            with patch.object(cli.asyncio, "sleep", new=AsyncMock(side_effect=KeyboardInterrupt)):
                                code = cli._cmd_gateway(
                                    channels="local",
                                    sender_id="u1",
                                    chat_id="c1",
                                    interactive_local=False,
                                )
        self.assertEqual(code, 0)
        self.assertTrue(state["constructed"])
        self.assertTrue(state["started"])
        self.assertTrue(state["stopped"])
        messages = [call.args[0] for call in mocked_warning.call_args_list if call.args]
        self.assertTrue(
            any("marked unavailable, gateway will continue without this MCP toolset" in msg for msg in messages)
        )

    def test_cmd_gateway_exits_when_whatsapp_bridge_precheck_fails(self) -> None:
        from openheron import cli

        fake_agent = pytypes.SimpleNamespace(name="openheron", tools=[])
        fake_agent_module = pytypes.SimpleNamespace(root_agent=fake_agent)

        class _UnexpectedGateway:
            def __init__(self, *args, **kwargs):
                raise AssertionError("Gateway should not be constructed when WhatsApp bridge precheck fails")

        fake_gateway_module = pytypes.SimpleNamespace(Gateway=_UnexpectedGateway)

        with patch.dict(
            sys.modules,
            {
                "openheron.agent": fake_agent_module,
                "openheron.gateway": fake_gateway_module,
            },
        ):
            with patch.object(cli, "parse_enabled_channels", return_value=["whatsapp"]):
                with patch.object(cli, "validate_channel_setup", return_value=[]):
                    with patch.object(cli, "_whatsapp_bridge_precheck_enabled", return_value=True):
                        with patch.object(
                            cli,
                            "_check_whatsapp_bridge_ready",
                            return_value="WhatsApp bridge precheck failed",
                        ):
                            with patch.object(cli.logger, "info") as mocked_info:
                                code = cli._cmd_gateway(
                                    channels="whatsapp",
                                    sender_id="u1",
                                    chat_id="c1",
                                    interactive_local=False,
                                )

        self.assertEqual(code, 1)
        messages = [call.args[0] for call in mocked_info.call_args_list if call.args]
        self.assertIn("[doctor] WhatsApp bridge precheck failed", messages)

    def test_cmd_onboard_creates_config_and_workspace(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}, clear=False):
                code = cli._cmd_onboard(force=False)

            self.assertEqual(code, 0)
            config_path = Path(tmp) / ".openheron" / "config.json"
            self.assertTrue(config_path.exists())
            data = json.loads(config_path.read_text(encoding="utf-8"))
            workspace = Path(data["agent"]["workspace"]).expanduser()
            self.assertTrue(workspace.exists())
            self.assertTrue((workspace / "skills").exists())

    def test_script_entrypoint_accepts_m(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        script_path = project_root / "openheron-cli"
        self.assertTrue(script_path.exists())

    def test_cmd_message_collects_final_text(self) -> None:
        from openheron import cli

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

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        fake_agent_module = pytypes.SimpleNamespace(root_agent=fake_agent)

        with patch.dict("sys.modules", {"openheron.agent": fake_agent_module}):
            with patch("openheron.cli.create_runner", return_value=(_FakeRunner(), object())):
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
        from openheron import cli

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

        fake_agent = pytypes.SimpleNamespace(name="openheron")
        fake_agent_module = pytypes.SimpleNamespace(root_agent=fake_agent)

        with patch.dict("sys.modules", {"openheron.agent": fake_agent_module}):
            with patch("openheron.cli.create_runner", return_value=(_FakeRunner(), object())):
                with patch.object(cli.logger, "info") as mocked_info:
                    code = cli._cmd_message("hello", user_id="u1", session_id="s1")

        self.assertEqual(code, 0)
        mocked_info.assert_called_with("hello world")

    def test_cron_list_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_cron_list", return_value=0) as mocked_list:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["cron", "list"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_list.assert_called_once_with(include_disabled=False)
                mocked_bootstrap.assert_called_once()

    def test_cron_add_dispatch_does_not_trigger_single_turn_message(self) -> None:
        from openheron import cli

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
        from openheron import cli

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
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"OPENHERON_WORKSPACE": tmp}, clear=False):
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
            store = Path(tmp) / ".openheron" / "cron_jobs.json"
            self.assertTrue(store.exists())

    def test_cmd_cron_run_reports_no_callback_in_plain_cli_process(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"OPENHERON_WORKSPACE": tmp}, clear=False):
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
        from openheron import cli

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
        from openheron import cli

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

    def test_cmd_doctor_reports_whatsapp_bridge_precheck_issue(self) -> None:
        from openheron import cli

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
                "OPENHERON_PROVIDER": "google",
                "OPENHERON_PROVIDER_ENABLED": "1",
                "GOOGLE_API_KEY": "k",
            },
            clear=False,
        ):
            with patch("openheron.cli.shutil.which", return_value="/usr/bin/adk"):
                with patch.object(cli, "validate_provider_runtime", return_value=None):
                    with patch.object(cli, "get_registry", return_value=fake_registry):
                        with patch.object(cli, "load_session_config", return_value=fake_session_cfg):
                            with patch.object(cli, "parse_enabled_channels", return_value=["whatsapp"]):
                                with patch.object(cli, "validate_channel_setup", return_value=[]):
                                    with patch.object(cli, "_whatsapp_bridge_precheck_enabled", return_value=True):
                                        with patch.object(
                                            cli,
                                            "_check_whatsapp_bridge_ready",
                                            return_value="WhatsApp bridge precheck failed",
                                        ):
                                            with patch.object(cli, "load_security_policy", return_value=fake_security_policy):
                                                with patch.object(cli, "build_mcp_toolsets_from_env", return_value=[]):
                                                    with patch.object(cli.logger, "debug"):
                                                        with patch("builtins.print") as mocked_print:
                                                            code = cli._cmd_doctor(output_json=True, verbose=False)

        self.assertEqual(code, 1)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertIn("WhatsApp bridge precheck failed", payload["issues"])

    def test_cmd_channels_login_rejects_unknown_channel(self) -> None:
        from openheron import cli

        with patch.object(cli.logger, "info") as mocked_info:
            code = cli._cmd_channels_login(channel_name="telegram")
        self.assertEqual(code, 1)
        self.assertIn("Unsupported channel", mocked_info.call_args[0][0])

    def test_cmd_channels_login_starts_bridge_with_token_from_config(self) -> None:
        from openheron import cli

        fake_cfg = {
            "channels": {
                "whatsapp": {
                    "bridgeToken": "bridge-token-1",
                }
            }
        }
        with patch.object(cli, "_get_bridge_dir", return_value=Path("/tmp/openheron-bridge")) as mocked_bridge:
            with patch.object(cli, "load_config", return_value=fake_cfg):
                with patch("openheron.cli.subprocess.run") as mocked_run:
                    code = cli._cmd_channels_login(channel_name="whatsapp")

        self.assertEqual(code, 0)
        mocked_bridge.assert_called_once_with()
        mocked_run.assert_called_once()
        call_args = mocked_run.call_args
        self.assertEqual(call_args.args[0], ["npm", "start"])
        self.assertEqual(call_args.kwargs["cwd"], Path("/tmp/openheron-bridge"))
        self.assertTrue(call_args.kwargs["check"])
        self.assertEqual(call_args.kwargs["env"]["BRIDGE_TOKEN"], "bridge-token-1")

    def test_cmd_channels_bridge_start_persists_runtime_state(self) -> None:
        from openheron import cli

        fake_cfg = {
            "channels": {
                "whatsapp": {
                    "bridgeToken": "bridge-token-2",
                }
            }
        }
        fake_proc = pytypes.SimpleNamespace(pid=54321)
        with tempfile.TemporaryDirectory() as tmp:
            runtime_base = Path(tmp) / "bridge-runtime"
            with patch.object(cli, "_bridge_base_dir", return_value=runtime_base):
                with patch.object(cli, "_get_bridge_dir", return_value=Path("/tmp/openheron-bridge")):
                    with patch.object(cli, "_is_pid_running", return_value=False):
                        with patch.object(cli, "load_config", return_value=fake_cfg):
                            with patch("openheron.cli.subprocess.Popen", return_value=fake_proc) as mocked_popen:
                                code = cli._cmd_channels_bridge_start(channel_name="whatsapp")

            self.assertEqual(code, 0)
            mocked_popen.assert_called_once()
            state_path = runtime_base / "runtime_state.json"
            self.assertTrue(state_path.exists())
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pid"], 54321)

    def test_cmd_channels_bridge_start_handles_bridge_dir_permission_error(self) -> None:
        from openheron import cli

        with patch.object(cli, "_get_bridge_dir", side_effect=PermissionError("no permission")):
            with patch.object(cli.logger, "info") as mocked_info:
                code = cli._cmd_channels_bridge_start(channel_name="whatsapp")
        self.assertEqual(code, 1)
        self.assertIn("Failed to prepare bridge directory", mocked_info.call_args[0][0])

    def test_cmd_channels_bridge_status_reports_running(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            runtime_base = Path(tmp) / "bridge-runtime"
            runtime_base.mkdir(parents=True, exist_ok=True)
            state_path = runtime_base / "runtime_state.json"
            state_path.write_text(json.dumps({"pid": 10001}), encoding="utf-8")
            with patch.object(cli, "_bridge_base_dir", return_value=runtime_base):
                with patch.object(cli, "_is_pid_running", return_value=True):
                    with patch.object(cli.logger, "info") as mocked_info:
                        code = cli._cmd_channels_bridge_status(channel_name="whatsapp")

        self.assertEqual(code, 0)
        self.assertIn("Bridge is running", mocked_info.call_args[0][0])

    def test_cmd_channels_bridge_stop_removes_stale_state(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            runtime_base = Path(tmp) / "bridge-runtime"
            runtime_base.mkdir(parents=True, exist_ok=True)
            state_path = runtime_base / "runtime_state.json"
            state_path.write_text(json.dumps({"pid": 10001}), encoding="utf-8")
            with patch.object(cli, "_bridge_base_dir", return_value=runtime_base):
                with patch.object(cli, "_is_pid_running", return_value=False):
                    with patch.object(cli.logger, "info"):
                        code = cli._cmd_channels_bridge_stop(channel_name="whatsapp")

            self.assertEqual(code, 0)
            self.assertFalse(state_path.exists())


if __name__ == "__main__":
    unittest.main()
