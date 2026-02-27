"""Tests for openheron CLI behavior."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import types as pytypes
import unittest
import asyncio
import io
import sys
import builtins
import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

_ORIGINAL_STDOUT: io.TextIOBase | None = None
_ORIGINAL_STDERR: io.TextIOBase | None = None
_CAPTURE_STDOUT: io.StringIO | None = None
_CAPTURE_STDERR: io.StringIO | None = None


def setUpModule() -> None:
    """Silence noisy CLI prints during test execution."""
    global _ORIGINAL_STDOUT, _ORIGINAL_STDERR, _CAPTURE_STDOUT, _CAPTURE_STDERR
    _ORIGINAL_STDOUT = sys.stdout
    _ORIGINAL_STDERR = sys.stderr
    _CAPTURE_STDOUT = io.StringIO()
    _CAPTURE_STDERR = io.StringIO()
    sys.stdout = _CAPTURE_STDOUT
    sys.stderr = _CAPTURE_STDERR


def tearDownModule() -> None:
    """Close leaked asyncio loops from optional channel dependencies."""
    global _ORIGINAL_STDOUT, _ORIGINAL_STDERR, _CAPTURE_STDOUT, _CAPTURE_STDERR

    try:
        import lark_oapi.ws.client as lark_ws_client  # type: ignore

        leaked = getattr(lark_ws_client, "loop", None)
        if leaked is not None and not leaked.is_running() and not leaked.is_closed():
            leaked.close()
    except Exception:
        pass

    # Fallback: close current default loop if one is still open.
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    except Exception:
        return
    try:
        if not loop.is_running() and not loop.is_closed():
            loop.close()
    finally:
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass
    if _ORIGINAL_STDOUT is not None:
        sys.stdout = _ORIGINAL_STDOUT
    if _ORIGINAL_STDERR is not None:
        sys.stderr = _ORIGINAL_STDERR
    _CAPTURE_STDOUT = None
    _CAPTURE_STDERR = None


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

    def test_onboard_command_removed(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["onboard"])
            self.assertEqual(ctx.exception.code, 2)
            mocked_bootstrap.assert_not_called()

    def test_install_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_install", return_value=0) as mocked_install:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["install", "--force", "--non-interactive"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_install.assert_called_once_with(
                    force=True,
                    non_interactive=True,
                    accept_risk=False,
                    install_daemon=False,
                    daemon_channels=None,
                    init_only=False,
                )
                mocked_bootstrap.assert_not_called()

    def test_install_mode_dispatch_with_daemon_flags(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_install", return_value=0) as mocked_install:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["install", "--install-daemon", "--daemon-channels", "local,feishu"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_install.assert_called_once_with(
                    force=False,
                    non_interactive=False,
                    accept_risk=False,
                    install_daemon=True,
                    daemon_channels="local,feishu",
                    init_only=False,
                )
                mocked_bootstrap.assert_not_called()

    def test_install_mode_dispatch_with_accept_risk(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_install", return_value=0) as mocked_install:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["install", "--non-interactive", "--accept-risk"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_install.assert_called_once_with(
                    force=False,
                    non_interactive=True,
                    accept_risk=True,
                    install_daemon=False,
                    daemon_channels=None,
                    init_only=False,
                )
                mocked_bootstrap.assert_not_called()

    def test_install_mode_dispatch_with_init_only(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_install", return_value=0) as mocked_install:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["install", "--init-only", "--force"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_install.assert_called_once_with(
                    force=True,
                    non_interactive=False,
                    accept_risk=False,
                    install_daemon=False,
                    daemon_channels=None,
                    init_only=True,
                )
                mocked_bootstrap.assert_not_called()

    def test_gateway_service_install_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_gateway_service_install", return_value=0) as mocked_install:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["gateway-service", "install", "--force", "--channels", "local,feishu", "--enable"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_install.assert_called_once_with(force=True, channels="local,feishu", enable=True)
                mocked_bootstrap.assert_called_once()

    def test_gateway_service_status_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_gateway_service_status", return_value=0) as mocked_status:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["gateway-service", "status", "--json"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_status.assert_called_once_with(output_json=True)
                mocked_bootstrap.assert_called_once()

    def test_gateway_start_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_gateway_start", return_value=0) as mocked_start:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["gateway", "start", "--channels", "local,feishu"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_start.assert_called_once_with(
                    channels="local,feishu",
                    sender_id="local-user",
                    chat_id="terminal",
                )
                mocked_bootstrap.assert_called_once()

    def test_gateway_status_mode_dispatch_json(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_gateway_status", return_value=0) as mocked_status:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["gateway", "status", "--json"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_status.assert_called_once_with(output_json=True)
                mocked_bootstrap.assert_called_once()

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
                mocked_doctor.assert_called_once_with(output_json=True, verbose=True, fix=False, fix_dry_run=False)

    def test_doctor_mode_passes_fix_flag(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config"):
            with patch.object(cli, "_cmd_doctor", return_value=0) as mocked_doctor:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["doctor", "--fix"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_doctor.assert_called_once_with(output_json=False, verbose=False, fix=True, fix_dry_run=False)

    def test_doctor_mode_passes_fix_dry_run_flag(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config"):
            with patch.object(cli, "_cmd_doctor", return_value=0) as mocked_doctor:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["doctor", "--fix-dry-run"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_doctor.assert_called_once_with(output_json=False, verbose=False, fix=True, fix_dry_run=True)

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

        with patch("builtins.print") as mocked_info:
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

    def test_routes_lint_mode_dispatch_with_json_and_limit(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_routes_lint", return_value=0) as mocked_lint:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["routes", "lint", "--json", "--limit", "12"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()
                mocked_lint.assert_called_once_with(output_json=True, limit=12)

    def test_routes_stats_mode_dispatch_with_json_and_limit(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_routes_stats", return_value=0) as mocked_stats:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["routes", "stats", "--json", "--limit", "9"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_bootstrap.assert_called_once()
                mocked_stats.assert_called_once_with(output_json=True, limit=9, window_hours=None)

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

        with patch("builtins.print") as mocked_info:
            code = cli._cmd_provider_login("openai")
        self.assertEqual(code, 1)
        self.assertIn("Unknown OAuth provider", mocked_info.call_args[0][0])

    def test_cmd_provider_login_invokes_registered_handler(self) -> None:
        from openheron import cli

        handler = Mock()
        with patch.dict(cli._PROVIDER_LOGIN_HANDLERS, {"openai_codex": handler}, clear=False):
            with patch("builtins.print"):
                code = cli._cmd_provider_login("openai-codex")
        self.assertEqual(code, 0)
        handler.assert_called_once_with()

    def test_cmd_provider_login_accepts_alias(self) -> None:
        from openheron import cli

        handler = Mock()
        with patch.dict(cli._PROVIDER_LOGIN_HANDLERS, {"openai_codex": handler}, clear=False):
            with patch("builtins.print"):
                code = cli._cmd_provider_login("codex")
        self.assertEqual(code, 0)
        handler.assert_called_once_with()

    def test_cmd_provider_login_openai_codex_uses_cached_valid_token(self) -> None:
        from openheron import cli

        token = pytypes.SimpleNamespace(access="token", account_id="acct_1")
        fake_provider = pytypes.SimpleNamespace(token_filename="openai_codex.json")
        fake_storage = Mock(name="storage")
        fake_oauth_module = pytypes.SimpleNamespace(
            get_token=Mock(return_value=token),
            login_oauth_interactive=Mock(return_value=token),
        )
        fake_oauth_providers = pytypes.SimpleNamespace(OPENAI_CODEX_PROVIDER=fake_provider)
        fake_oauth_storage = pytypes.SimpleNamespace(FileTokenStorage=Mock(return_value=fake_storage))
        with patch.dict(
            sys.modules,
            {
                "oauth_cli_kit": fake_oauth_module,
                "oauth_cli_kit.providers": fake_oauth_providers,
                "oauth_cli_kit.storage": fake_oauth_storage,
            },
        ):
            with patch("builtins.print"):
                code = cli._cmd_provider_login("openai-codex")
        self.assertEqual(code, 0)
        self.assertGreaterEqual(fake_oauth_module.get_token.call_count, 1)
        fake_oauth_module.login_oauth_interactive.assert_not_called()

    def test_cmd_provider_login_openai_codex_rejects_missing_account_id(self) -> None:
        from openheron import cli

        token = pytypes.SimpleNamespace(access="token", account_id="")
        fake_provider = pytypes.SimpleNamespace(token_filename="openai_codex.json")
        fake_storage = Mock(name="storage")
        fake_oauth_module = pytypes.SimpleNamespace(
            get_token=Mock(return_value=token),
            login_oauth_interactive=Mock(return_value=token),
        )
        fake_oauth_providers = pytypes.SimpleNamespace(OPENAI_CODEX_PROVIDER=fake_provider)
        fake_oauth_storage = pytypes.SimpleNamespace(FileTokenStorage=Mock(return_value=fake_storage))
        with patch.dict(
            sys.modules,
            {
                "oauth_cli_kit": fake_oauth_module,
                "oauth_cli_kit.providers": fake_oauth_providers,
                "oauth_cli_kit.storage": fake_oauth_storage,
            },
        ):
            with patch("builtins.print") as mocked_info:
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

    def test_doctor_validate_multi_agent_config_accepts_default_config(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        issues = cli._doctor_validate_multi_agent_config(cfg)
        self.assertEqual(issues, [])

    def test_doctor_validate_multi_agent_config_reports_binding_and_default_errors(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        cfg["agents"]["list"] = [
            {"id": "main", "default": True},
            {"id": "main", "default": True},
        ]
        cfg["bindings"] = [
            {"agentId": "missing", "match": {"channel": "whatsapp"}},
            {"agentId": "main", "match": {}},
        ]
        issues = cli._doctor_validate_multi_agent_config(cfg)
        joined = "\n".join(issues)
        self.assertIn("duplicate agent id 'main'", joined)
        self.assertIn("exactly one default agent is required (found 2)", joined)
        self.assertIn("agentId 'missing' does not exist", joined)
        self.assertIn("match.channel is required", joined)

    def test_doctor_validate_multi_agent_config_reports_invalid_scope_shapes(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        cfg["agents"]["list"] = [{"id": "main", "default": True}]
        cfg["bindings"] = [
            {
                "agentId": "main",
                "match": {
                    "channel": "discord",
                    "guild": {"id": ""},
                    "team": "dev",
                    "roles": [],
                },
            }
        ]
        issues = cli._doctor_validate_multi_agent_config(cfg)
        joined = "\n".join(issues)
        self.assertIn("match.guild.id is required", joined)
        self.assertIn("match.team must be an object", joined)
        self.assertIn("match.roles must contain at least one non-empty value", joined)

    def test_doctor_summarize_multi_agent_config_counts_by_channel_and_tier(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        cfg["agents"]["list"] = [
            {"id": "main", "default": True},
            {"id": "biz"},
        ]
        cfg["bindings"] = [
            {"agentId": "main", "match": {"channel": "telegram"}},
            {"agentId": "biz", "match": {"channel": "whatsapp", "accountId": "business"}},
            {
                "agentId": "biz",
                "match": {"channel": "whatsapp", "accountId": "business", "peer": {"kind": "direct", "id": "+1"}},
            },
        ]
        summary = cli._doctor_summarize_multi_agent_config(cfg)
        self.assertEqual(summary["agentCount"], 2)
        self.assertEqual(summary["bindingCount"], 3)
        self.assertEqual(summary["byChannel"]["whatsapp"], 2)
        self.assertEqual(summary["routeTierCount"]["channel"], 1)
        self.assertEqual(summary["routeTierCount"]["account"], 1)
        self.assertEqual(summary["routeTierCount"]["peer"], 1)
        self.assertEqual(summary["routeScopeCount"]["guild"], 0)
        self.assertEqual(summary["routeScopeCount"]["team"], 0)
        self.assertEqual(summary["routeScopeCount"]["roles"], 0)
        self.assertEqual(summary["conflicts"], [])

    def test_doctor_summarize_multi_agent_config_detects_duplicate_binding_signature(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        cfg["agents"]["list"] = [
            {"id": "main", "default": True},
            {"id": "biz"},
        ]
        cfg["bindings"] = [
            {"agentId": "main", "match": {"channel": "whatsapp", "accountId": "business"}},
            {"agentId": "biz", "match": {"channel": "whatsapp", "accountId": "business"}},
        ]
        summary = cli._doctor_summarize_multi_agent_config(cfg)
        self.assertEqual(len(summary["conflicts"]), 1)
        self.assertIn("duplicates route match", summary["conflicts"][0])

    def test_doctor_summarize_multi_agent_config_distinguishes_roles_scope(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        cfg["agents"]["list"] = [{"id": "main", "default": True}, {"id": "ops"}, {"id": "sales"}]
        cfg["bindings"] = [
            {
                "agentId": "ops",
                "match": {"channel": "discord", "guild": {"id": "g1"}, "roles": ["admin"]},
            },
            {
                "agentId": "sales",
                "match": {"channel": "discord", "guild": {"id": "g1"}, "roles": ["member"]},
            },
        ]
        summary = cli._doctor_summarize_multi_agent_config(cfg)
        self.assertEqual(summary["routeScopeCount"]["guild"], 2)
        self.assertEqual(summary["routeScopeCount"]["roles"], 2)
        self.assertEqual(summary["conflicts"], [])

    def test_doctor_preview_multi_agent_routes_builds_binding_and_default_examples(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        cfg["agents"]["list"] = [
            {"id": "main", "default": True},
            {"id": "biz"},
        ]
        cfg["bindings"] = [
            {"agentId": "biz", "match": {"channel": "whatsapp", "accountId": "business"}},
            {
                "agentId": "biz",
                "match": {"channel": "whatsapp", "accountId": "business", "peer": {"kind": "direct", "id": "+1"}},
            },
        ]
        preview = cli._doctor_preview_multi_agent_routes(cfg, limit=8)
        self.assertGreaterEqual(len(preview), 3)
        self.assertEqual(preview[0]["type"], "binding")
        self.assertIn("agent:biz:whatsapp:business:direct:sample-peer", preview[0]["sessionIdExample"])
        self.assertEqual(preview[0]["guildId"], "")
        self.assertEqual(preview[0]["roles"], [])
        self.assertEqual(preview[1]["tier"], "peer")
        self.assertEqual(preview[-1]["type"], "default")
        self.assertIn("agent:main:local:default:direct:sample-peer", preview[-1]["sessionIdExample"])

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
            with patch("openheron.app.cli.shutil.which", return_value="/usr/bin/adk"):
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
                                                        with patch("builtins.print") as mocked_info:
                                                            code = cli._cmd_doctor(output_json=False, verbose=False)

        self.assertEqual(code, 1)
        info_text = "\n".join(call.args[0] for call in mocked_info.call_args_list if call.args)
        self.assertIn("Issues:", info_text)
        self.assertIn("MCP server 'filesystem' health check failed", info_text)

    def test_cmd_install_runs_init_setup_and_doctor(self) -> None:
        from openheron import cli

        with patch.object(cli, "_cmd_install_init_setup", return_value=0) as mocked_init:
            with patch.object(cli, "_cmd_doctor", return_value=0) as mocked_doctor:
                with patch.object(cli, "_cmd_gateway_service_install", return_value=0) as mocked_daemon:
                    with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
                        with patch("builtins.print"):
                            code = cli._cmd_install(force=False, non_interactive=False)

        self.assertEqual(code, 0)
        mocked_init.assert_called_once_with(force=False)
        mocked_doctor.assert_called_once_with(output_json=False, verbose=False)
        mocked_daemon.assert_not_called()
        mocked_bootstrap.assert_called_once()

    def test_cmd_install_init_only_runs_init_setup_only(self) -> None:
        from openheron import cli

        with patch.object(cli, "_cmd_install_init_setup", return_value=0) as mocked_init:
            with patch.object(cli, "_cmd_doctor", return_value=0) as mocked_doctor:
                with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
                    with patch("builtins.print"):
                        code = cli._cmd_install(force=False, non_interactive=False, init_only=True)

        self.assertEqual(code, 0)
        mocked_init.assert_called_once_with(force=False)
        mocked_doctor.assert_not_called()
        mocked_bootstrap.assert_not_called()

    def test_cmd_install_init_only_rejects_conflicting_flags(self) -> None:
        from openheron import cli

        with patch.object(cli, "_cmd_install_init_setup", return_value=0) as mocked_init:
            with patch("builtins.print") as mocked_print:
                code = cli._cmd_install(
                    force=False,
                    non_interactive=True,
                    accept_risk=True,
                    install_daemon=True,
                    daemon_channels="local",
                    init_only=True,
                )

        self.assertEqual(code, 1)
        mocked_init.assert_not_called()
        lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
        self.assertTrue(any("init-only mode cannot be combined" in line for line in lines))

    def test_cmd_install_with_daemon_calls_gateway_service_install(self) -> None:
        from openheron import cli

        with patch.object(cli, "_cmd_install_init_setup", return_value=0):
            with patch.object(cli, "_cmd_doctor", return_value=0):
                with patch.object(cli, "_cmd_gateway_service_install", return_value=0) as mocked_daemon:
                    with patch.object(cli, "bootstrap_env_from_config"):
                        with patch("builtins.print"):
                            code = cli._cmd_install(
                                force=False,
                                non_interactive=False,
                                accept_risk=False,
                                install_daemon=True,
                                daemon_channels="local,feishu",
                            )

        self.assertEqual(code, 0)
        mocked_daemon.assert_called_once_with(force=False, channels="local,feishu", enable=True)

    def test_cmd_install_with_daemon_failure_does_not_block_install(self) -> None:
        from openheron import cli

        with patch.object(cli, "_cmd_install_init_setup", return_value=0):
            with patch.object(cli, "_cmd_doctor", return_value=0):
                with patch.object(cli, "_cmd_gateway_service_install", return_value=1):
                    with patch.object(cli, "bootstrap_env_from_config"):
                        with patch("builtins.print") as mocked_print:
                            code = cli._cmd_install(
                                force=False,
                                non_interactive=False,
                                accept_risk=False,
                                install_daemon=True,
                                daemon_channels="local",
                            )

        self.assertEqual(code, 0)
        lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
        self.assertTrue(any("Install daemon setup failed." in line for line in lines))
        self.assertTrue(any("Install daemon retry:" in line for line in lines))

    def test_cmd_install_updates_provider_from_interactive_setup(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cli.save_config(cli.default_config(), config_path=config_path)
            with patch.object(cli, "_cmd_install_init_setup", return_value=0):
                with patch.object(cli, "_cmd_doctor", return_value=0):
                    with patch.object(cli, "get_config_path", return_value=config_path):
                        with patch.object(cli, "bootstrap_env_from_config"):
                            with patch("sys.stdin.isatty", return_value=True):
                                with patch("builtins.input", side_effect=["openai", "test-api-key", ""]):
                                    with patch("builtins.print"):
                                        code = cli._cmd_install(force=False, non_interactive=False)

            updated = cli.load_config(config_path=config_path)

        self.assertEqual(code, 0)
        self.assertTrue(updated["providers"]["openai"]["enabled"])
        self.assertFalse(updated["providers"]["google"]["enabled"])
        self.assertEqual(updated["providers"]["openai"]["apiKey"], "test-api-key")

    def test_install_interactive_setup_retries_unknown_provider(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cli.save_config(cli.default_config(), config_path=config_path)
            answers = iter(["unknown-provider", "openai", "", ""])
            with patch("builtins.print"):
                cli._run_install_interactive_setup(
                    config_path=config_path,
                    input_fn=lambda _prompt: next(answers),
                )
            updated = cli.load_config(config_path=config_path)

        self.assertTrue(updated["providers"]["openai"]["enabled"])
        self.assertFalse(updated["providers"]["google"]["enabled"])

    def test_install_interactive_setup_updates_enabled_channels(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["channels"]["local"]["enabled"] = True
            cfg["channels"]["feishu"]["enabled"] = False
            cfg["channels"]["telegram"]["enabled"] = False
            cli.save_config(cfg, config_path=config_path)
            answers = iter(["skip", "", "local,feishu", "", ""])
            with patch("builtins.print"):
                cli._run_install_interactive_setup(
                    config_path=config_path,
                    input_fn=lambda _prompt: next(answers),
                )
            updated = cli.load_config(config_path=config_path)

        self.assertTrue(updated["channels"]["local"]["enabled"])
        self.assertTrue(updated["channels"]["feishu"]["enabled"])
        self.assertFalse(updated["channels"]["telegram"]["enabled"])

    def test_install_interactive_setup_collects_channel_credentials(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["channels"]["local"]["enabled"] = True
            cfg["channels"]["feishu"]["enabled"] = False
            cli.save_config(cfg, config_path=config_path)
            answers = iter(["skip", "", "feishu", "feishu-app-id", "feishu-app-secret"])
            with patch("builtins.print"):
                cli._run_install_interactive_setup(
                    config_path=config_path,
                    input_fn=lambda _prompt: next(answers),
                )
            updated = cli.load_config(config_path=config_path)

        self.assertTrue(updated["channels"]["feishu"]["enabled"])
        self.assertEqual(updated["channels"]["feishu"]["appId"], "feishu-app-id")
        self.assertEqual(updated["channels"]["feishu"]["appSecret"], "feishu-app-secret")

    def test_install_interactive_setup_collects_dingtalk_and_slack_credentials(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["channels"]["local"]["enabled"] = True
            cfg["channels"]["dingtalk"]["enabled"] = False
            cfg["channels"]["slack"]["enabled"] = False
            cli.save_config(cfg, config_path=config_path)
            answers = iter(["skip", "", "dingtalk,slack", "dingtalk-id", "dingtalk-secret", "slack-token"])
            with patch("builtins.print"):
                cli._run_install_interactive_setup(
                    config_path=config_path,
                    input_fn=lambda _prompt: next(answers),
                )
            updated = cli.load_config(config_path=config_path)

        self.assertTrue(updated["channels"]["dingtalk"]["enabled"])
        self.assertEqual(updated["channels"]["dingtalk"]["clientId"], "dingtalk-id")
        self.assertEqual(updated["channels"]["dingtalk"]["clientSecret"], "dingtalk-secret")
        self.assertTrue(updated["channels"]["slack"]["enabled"])
        self.assertEqual(updated["channels"]["slack"]["botToken"], "slack-token")

    def test_install_interactive_setup_collects_whatsapp_mochat_and_email_credentials(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["channels"]["local"]["enabled"] = True
            cfg["channels"]["whatsapp"]["enabled"] = False
            cfg["channels"]["mochat"]["enabled"] = False
            cfg["channels"]["email"]["enabled"] = False
            cfg["channels"]["whatsapp"]["bridgeUrl"] = ""
            cfg["channels"]["mochat"]["baseUrl"] = ""
            cfg["channels"]["mochat"]["clawToken"] = ""
            cfg["channels"]["email"]["consentGranted"] = False
            cfg["channels"]["email"]["smtpHost"] = ""
            cfg["channels"]["email"]["smtpUsername"] = ""
            cfg["channels"]["email"]["smtpPassword"] = ""
            cli.save_config(cfg, config_path=config_path)
            answers = iter(
                [
                    "skip",
                    "",
                    "whatsapp,mochat,email",
                    "ws://bridge.local",
                    "https://mochat.local",
                    "mochat-token",
                    "yes",
                    "smtp.local",
                    "mailer",
                    "smtp-secret",
                ]
            )
            with patch("builtins.print"):
                cli._run_install_interactive_setup(
                    config_path=config_path,
                    input_fn=lambda _prompt: next(answers),
                )
            updated = cli.load_config(config_path=config_path)

        self.assertTrue(updated["channels"]["whatsapp"]["enabled"])
        self.assertEqual(updated["channels"]["whatsapp"]["bridgeUrl"], "ws://bridge.local")
        self.assertTrue(updated["channels"]["mochat"]["enabled"])
        self.assertEqual(updated["channels"]["mochat"]["baseUrl"], "https://mochat.local")
        self.assertEqual(updated["channels"]["mochat"]["clawToken"], "mochat-token")
        self.assertTrue(updated["channels"]["email"]["enabled"])
        self.assertTrue(updated["channels"]["email"]["consentGranted"])
        self.assertEqual(updated["channels"]["email"]["smtpHost"], "smtp.local")
        self.assertEqual(updated["channels"]["email"]["smtpUsername"], "mailer")
        self.assertEqual(updated["channels"]["email"]["smtpPassword"], "smtp-secret")

    def test_install_interactive_setup_required_channel_prompts_are_explicit(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["channels"]["local"]["enabled"] = True
            cfg["channels"]["telegram"]["enabled"] = False
            cfg["channels"]["telegram"]["token"] = ""
            cli.save_config(cfg, config_path=config_path)
            prompts: list[str] = []
            answers = iter(["skip", "", "telegram", ""])

            def _input(prompt: str) -> str:
                prompts.append(prompt)
                return next(answers)

            with patch("builtins.print"):
                cli._run_install_interactive_setup(
                    config_path=config_path,
                    input_fn=_input,
                )

        self.assertTrue(
            any(
                "Telegram bot token (required for enabled channel, press Enter to skip for now)> " in prompt
                for prompt in prompts
            )
        )

    def test_install_interactive_setup_collects_qq_credentials(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["channels"]["local"]["enabled"] = True
            cfg["channels"]["qq"]["enabled"] = False
            cfg["channels"]["qq"]["appId"] = ""
            cfg["channels"]["qq"]["secret"] = ""
            cli.save_config(cfg, config_path=config_path)
            answers = iter(["skip", "", "qq", "qq-app-id", "qq-secret"])
            with patch("builtins.print"):
                cli._run_install_interactive_setup(
                    config_path=config_path,
                    input_fn=lambda _prompt: next(answers),
                )
            updated = cli.load_config(config_path=config_path)

        self.assertTrue(updated["channels"]["qq"]["enabled"])
        self.assertEqual(updated["channels"]["qq"]["appId"], "qq-app-id")
        self.assertEqual(updated["channels"]["qq"]["secret"], "qq-secret")

    def test_install_interactive_setup_uses_provider_adapter_when_available(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["providers"]["google"]["enabled"] = True
            cfg["providers"]["google"]["apiKey"] = ""
            cli.save_config(cfg, config_path=config_path)

            adapter = pytypes.SimpleNamespace(provider_name="google")
            adapter.collect_credentials = Mock(
                side_effect=lambda **kwargs: kwargs["provider_cfg"].update({"apiKey": "from-adapter"})
            )
            answers = iter(["skip", "", ""])
            with patch.object(cli, "resolve_provider_onboarding_adapter", return_value=adapter):
                with patch("builtins.print"):
                    cli._run_install_interactive_setup(
                        config_path=config_path,
                        input_fn=lambda _prompt: next(answers),
                    )
            updated = cli.load_config(config_path=config_path)

        adapter.collect_credentials.assert_called_once()
        self.assertEqual(updated["providers"]["google"]["apiKey"], "from-adapter")

    def test_install_interactive_setup_uses_channel_adapter_when_available(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["channels"]["local"]["enabled"] = False
            cfg["channels"]["telegram"]["enabled"] = True
            cfg["channels"]["telegram"]["token"] = ""
            cli.save_config(cfg, config_path=config_path)

            adapter = pytypes.SimpleNamespace(channel_name="telegram")
            adapter.collect_credentials = Mock(
                side_effect=lambda **kwargs: kwargs["channel_cfg"].update({"token": "adapter-token"})
            )
            answers = iter(["skip", "", ""])
            with patch.object(
                cli,
                "resolve_channel_onboarding_adapter",
                side_effect=lambda name: adapter if name == "telegram" else None,
            ):
                with patch("builtins.print"):
                    cli._run_install_interactive_setup(
                        config_path=config_path,
                        input_fn=lambda _prompt: next(answers),
                    )
            updated = cli.load_config(config_path=config_path)

        adapter.collect_credentials.assert_called_once()
        self.assertEqual(updated["channels"]["telegram"]["token"], "adapter-token")

    def test_review_install_missing_fields_can_fill_provider_and_channel(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        cfg["providers"]["google"]["enabled"] = True
        cfg["providers"]["google"]["apiKey"] = ""
        cfg["channels"]["local"]["enabled"] = False
        cfg["channels"]["feishu"]["enabled"] = True
        cfg["channels"]["feishu"]["appId"] = ""
        cfg["channels"]["feishu"]["appSecret"] = ""
        answers = iter(["y", "google-key", "feishu-app-id", "feishu-app-secret"])

        with patch("builtins.print"):
            cli._review_install_missing_fields(
                providers_cfg=cfg["providers"],
                channels_cfg=cfg["channels"],
                input_fn=lambda _prompt: next(answers),
            )

        self.assertEqual(cfg["providers"]["google"]["apiKey"], "google-key")
        self.assertEqual(cfg["channels"]["feishu"]["appId"], "feishu-app-id")
        self.assertEqual(cfg["channels"]["feishu"]["appSecret"], "feishu-app-secret")

    def test_review_install_missing_fields_respects_skip_choice(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        cfg["providers"]["google"]["enabled"] = True
        cfg["providers"]["google"]["apiKey"] = ""
        answers = iter(["n"])

        with patch("builtins.print"):
            cli._review_install_missing_fields(
                providers_cfg=cfg["providers"],
                channels_cfg=cfg["channels"],
                input_fn=lambda _prompt: next(answers),
            )

        self.assertEqual(cfg["providers"]["google"]["apiKey"], "")

    def test_cmd_install_skips_interactive_setup_in_non_tty(self) -> None:
        from openheron import cli

        with patch.object(cli, "_cmd_install_init_setup", return_value=0):
            with patch.object(cli, "_cmd_doctor", return_value=0):
                with patch.object(cli, "bootstrap_env_from_config"):
                    with patch("sys.stdin.isatty", return_value=False):
                        with patch("builtins.print") as mocked_print:
                            code = cli._cmd_install(force=False, non_interactive=False)

        self.assertEqual(code, 0)
        lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
        self.assertTrue(any("Install setup skipped: non-interactive terminal." in line for line in lines))

    def test_cmd_install_non_interactive_skips_setup_prompt(self) -> None:
        from openheron import cli

        with patch.object(cli, "_cmd_install_init_setup", return_value=0):
            with patch.object(cli, "_cmd_doctor", return_value=0):
                with patch.object(cli, "bootstrap_env_from_config"):
                    with patch("builtins.input") as mocked_input:
                        with patch("builtins.print"):
                            code = cli._cmd_install(force=False, non_interactive=True, accept_risk=True)

        self.assertEqual(code, 0)
        mocked_input.assert_not_called()

    def test_cmd_install_non_interactive_requires_accept_risk(self) -> None:
        from openheron import cli

        with patch.object(cli, "_cmd_install_init_setup", return_value=0) as mocked_init:
            with patch.object(cli, "_cmd_doctor", return_value=0) as mocked_doctor:
                with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
                    with patch("builtins.print") as mocked_print:
                        code = cli._cmd_install(force=False, non_interactive=True, accept_risk=False)

        self.assertEqual(code, 1)
        mocked_init.assert_not_called()
        mocked_doctor.assert_not_called()
        mocked_bootstrap.assert_not_called()
        lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
        self.assertTrue(any("Non-interactive install requires explicit risk acknowledgement." in line for line in lines))
        self.assertTrue(any("--accept-risk" in line for line in lines))

    def test_install_summary_lines_reports_missing_required_fields(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["providers"]["google"]["enabled"] = True
            cfg["providers"]["google"]["apiKey"] = ""
            cfg["channels"]["feishu"]["enabled"] = True
            cfg["channels"]["feishu"]["appId"] = ""
            cfg["channels"]["feishu"]["appSecret"] = ""
            cli.save_config(cfg, config_path=config_path)

            lines = cli._install_summary_lines(config_path)

        self.assertIn("provider=google", lines[0])
        self.assertIn("channels.feishu.appId", lines[1])
        self.assertIn("channels.feishu.appSecret", lines[1])
        self.assertIn("Feishu credentials", lines[2])
        self.assertIn("next[1]=openheron doctor", lines[3])
        self.assertIn("next[2]=openheron gateway --channels local,feishu", lines[4])

    def test_install_summary_lines_reports_next_commands_when_ready(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["providers"]["google"]["enabled"] = True
            cfg["providers"]["google"]["apiKey"] = "k"
            cfg["channels"]["local"]["enabled"] = True
            cfg["channels"]["feishu"]["enabled"] = False
            cfg["channels"]["telegram"]["enabled"] = False
            cli.save_config(cfg, config_path=config_path)

            lines = cli._install_summary_lines(config_path)

        self.assertIn("no required fields missing", lines[1])
        self.assertIn("next[1]=openheron doctor", lines[2])
        self.assertIn("next[2]=openheron gateway --channels local", lines[3])

    def test_install_summary_lines_reports_missing_dingtalk_and_slack_credentials(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["channels"]["local"]["enabled"] = False
            cfg["channels"]["dingtalk"]["enabled"] = True
            cfg["channels"]["slack"]["enabled"] = True
            cli.save_config(cfg, config_path=config_path)

            lines = cli._install_summary_lines(config_path)

        joined = "\n".join(lines)
        self.assertIn("channels.dingtalk.clientId", joined)
        self.assertIn("channels.dingtalk.clientSecret", joined)
        self.assertIn("channels.slack.botToken", joined)

    def test_install_summary_lines_reports_missing_whatsapp_mochat_and_email_credentials(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["channels"]["local"]["enabled"] = False
            cfg["channels"]["whatsapp"]["enabled"] = True
            cfg["channels"]["whatsapp"]["bridgeUrl"] = ""
            cfg["channels"]["mochat"]["enabled"] = True
            cfg["channels"]["mochat"]["baseUrl"] = ""
            cfg["channels"]["mochat"]["clawToken"] = ""
            cfg["channels"]["email"]["enabled"] = True
            cfg["channels"]["email"]["consentGranted"] = False
            cfg["channels"]["email"]["smtpHost"] = ""
            cfg["channels"]["email"]["smtpUsername"] = ""
            cfg["channels"]["email"]["smtpPassword"] = ""
            cli.save_config(cfg, config_path=config_path)

            lines = cli._install_summary_lines(config_path)

        joined = "\n".join(lines)
        self.assertIn("channels.whatsapp.bridgeUrl", joined)
        self.assertIn("channels.mochat.baseUrl", joined)
        self.assertIn("channels.mochat.clawToken", joined)
        self.assertIn("channels.email.consentGranted", joined)
        self.assertIn("channels.email.smtpHost", joined)
        self.assertIn("channels.email.smtpUsername", joined)
        self.assertIn("channels.email.smtpPassword", joined)

    def test_install_summary_lines_reports_missing_qq_credentials(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["channels"]["local"]["enabled"] = False
            cfg["channels"]["qq"]["enabled"] = True
            cfg["channels"]["qq"]["appId"] = ""
            cfg["channels"]["qq"]["secret"] = ""
            cli.save_config(cfg, config_path=config_path)

            lines = cli._install_summary_lines(config_path)

        joined = "\n".join(lines)
        self.assertIn("channels.qq.appId", joined)
        self.assertIn("channels.qq.secret", joined)

    def test_install_summary_schema_includes_email_consent_and_password_semantics(self) -> None:
        from openheron import cli

        requirements = cli.INSTALL_CHANNEL_SUMMARY_REQUIREMENTS
        consent_rule = next(
            (
                item
                for item in requirements
                if item.channel == "email" and item.key == "consentGranted"
            ),
            None,
        )
        password_rule = next(
            (
                item
                for item in requirements
                if item.channel == "email" and item.key == "smtpPassword"
            ),
            None,
        )
        self.assertIsNotNone(consent_rule)
        self.assertIsNotNone(password_rule)
        self.assertEqual(consent_rule.presence, "truthy_bool")
        self.assertEqual(password_rule.presence, "non_empty_raw")

    def test_install_summary_provider_schema_contains_api_key_requirement(self) -> None:
        from openheron import cli

        api_key_rule = next(
            (
                item
                for item in cli.INSTALL_PROVIDER_SUMMARY_REQUIREMENTS
                if item.key == "apiKey"
            ),
            None,
        )
        self.assertIsNotNone(api_key_rule)
        self.assertTrue(api_key_rule.skip_for_oauth)
        self.assertIsNotNone(api_key_rule.env_name_resolver)
        self.assertEqual(api_key_rule.doctor_env_backfill_code, "provider.env.api_key_backfilled")

    def test_doctor_channel_backfill_schema_matches_channel_env_mappings(self) -> None:
        from openheron import cli

        self.assertEqual(
            len(cli.DOCTOR_CHANNEL_ENV_BACKFILL_RULES),
            len(cli.CHANNEL_ENV_BACKFILL_MAPPINGS),
        )
        telegram_rule = next(
            (
                item
                for item in cli.DOCTOR_CHANNEL_ENV_BACKFILL_RULES
                if item.channel == "telegram" and item.key == "token"
            ),
            None,
        )
        self.assertIsNotNone(telegram_rule)
        self.assertEqual(telegram_rule.env_name, "TELEGRAM_BOT_TOKEN")
        self.assertEqual(telegram_rule.code_applied, "channel.env.backfilled")
        self.assertEqual(telegram_rule.rule, "channel_env_backfill")

    def test_doctor_channel_bool_backfill_schema_contains_email_consent_rule(self) -> None:
        from openheron import cli

        consent_rule = next(
            (
                item
                for item in cli.DOCTOR_CHANNEL_BOOL_ENV_BACKFILL_RULES
                if item.channel == "email" and item.key == "consentGranted"
            ),
            None,
        )
        self.assertIsNotNone(consent_rule)
        self.assertEqual(consent_rule.env_name, "EMAIL_CONSENT_GRANTED")
        self.assertEqual(consent_rule.code_applied, "email.consent.backfilled")
        self.assertEqual(consent_rule.rule, "email_consent_backfill")

    def test_install_summary_requirements_cover_doctor_channel_backfill_targets(self) -> None:
        from openheron import cli

        summary_targets = {
            f"{item.channel}.{item.key}"
            for item in cli.INSTALL_CHANNEL_SUMMARY_REQUIREMENTS
        }
        doctor_targets = {
            f"{item.channel}.{item.key}"
            for item in cli.DOCTOR_CHANNEL_ENV_BACKFILL_RULES
        }
        self.assertTrue(doctor_targets.issubset(summary_targets))
        self.assertIn("email.consentGranted", summary_targets)

    def test_cmd_install_prints_summary_lines(self) -> None:
        from openheron import cli

        with patch.object(cli, "_cmd_install_init_setup", return_value=0):
            with patch.object(cli, "_cmd_doctor", return_value=0):
                with patch.object(cli, "bootstrap_env_from_config"):
                    with patch.object(cli, "_install_summary_lines", return_value=["s1", "s2"]):
                        with patch.object(cli, "_install_prereq_lines", return_value=["p1"]):
                            with patch("sys.stdin.isatty", return_value=False):
                                with patch("builtins.print") as mocked_print:
                                    code = cli._cmd_install(force=False, non_interactive=False)

        self.assertEqual(code, 0)
        lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
        self.assertIn("s1", lines)
        self.assertIn("s2", lines)
        self.assertIn("p1", lines)

    def test_install_prereq_lines_report_missing_tools(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cli.Path, "cwd", return_value=Path(tmp)):
                with patch.object(cli.shutil, "which", return_value=None):
                    with patch.object(cli.importlib.util, "find_spec", return_value=None):
                        lines = cli._install_prereq_lines()

        merged = "\n".join(lines)
        self.assertIn(".venv not found", merged)
        self.assertIn("adk CLI not found", merged)
        self.assertIn("questionary missing", merged)
        self.assertIn("rich missing", merged)

    def test_install_prereq_lines_report_detected_tools(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            venv_python = Path(tmp) / ".venv" / "bin"
            venv_python.mkdir(parents=True, exist_ok=True)
            (venv_python / "python").write_text("", encoding="utf-8")
            with patch.object(cli.Path, "cwd", return_value=Path(tmp)):
                with patch.object(cli.shutil, "which", return_value="/usr/local/bin/adk"):
                    with patch.object(cli.importlib.util, "find_spec", return_value=object()):
                        lines = cli._install_prereq_lines()

        merged = "\n".join(lines)
        self.assertIn("virtualenv detected", merged)
        self.assertIn("adk CLI detected", merged)
        self.assertIn("questionary detected", merged)
        self.assertIn("rich detected", merged)

    def test_doctor_install_prereq_line_normalizes_prefix_and_status(self) -> None:
        from openheron import cli

        ok_line = cli._doctor_install_prereq_line("Install prereq: virtualenv detected at /tmp/.venv")
        warn_line = cli._doctor_install_prereq_line("Install prereq: adk CLI not found")

        self.assertEqual(ok_line, "Install prereq [ok]: virtualenv detected at /tmp/.venv")
        self.assertEqual(warn_line, "Install prereq [warn]: adk CLI not found")

    def test_gui_execution_path_hint_variants(self) -> None:
        from openheron import cli

        self.assertEqual(
            cli._gui_execution_path_hint(
                builtin_tools_enabled=True,
                gui_task_tool="mcp_gui_gui_task",
                gui_action_tool="mcp_gui_gui_action",
            )[0],
            "hybrid_prefer_mcp",
        )
        self.assertEqual(
            cli._gui_execution_path_hint(
                builtin_tools_enabled=True,
                gui_task_tool=None,
                gui_action_tool=None,
            )[0],
            "builtin_only",
        )
        self.assertEqual(
            cli._gui_execution_path_hint(
                builtin_tools_enabled=False,
                gui_task_tool="mcp_gui_gui_task",
                gui_action_tool="mcp_gui_gui_action",
            )[0],
            "mcp_only",
        )
        self.assertEqual(
            cli._gui_execution_path_hint(
                builtin_tools_enabled=False,
                gui_task_tool=None,
                gui_action_tool=None,
            )[0],
            "disabled",
        )

    def test_cmd_gateway_service_install_writes_launchd_manifest(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            data = Path(tmp) / "data"
            home.mkdir(parents=True, exist_ok=True)
            data.mkdir(parents=True, exist_ok=True)
            with patch.object(cli, "detect_service_manager", return_value="launchd"):
                with patch.object(cli.Path, "home", return_value=home):
                    with patch.object(cli, "get_data_dir", return_value=data):
                        with patch.object(cli, "get_config_path", return_value=data / "config.json"):
                            with patch.object(cli, "load_config", return_value=cli.default_config()):
                                with patch.object(cli.shutil, "which", return_value="/usr/local/bin/openheron"):
                                    with patch("builtins.print") as mocked_print:
                                        code = cli._cmd_gateway_service_install(
                                            force=False,
                                            channels="local",
                                            enable=False,
                                        )
            self.assertEqual(code, 0)
            manifest_path = home / "Library" / "LaunchAgents" / "openheron-gateway.plist"
            self.assertTrue(manifest_path.exists())
            content = manifest_path.read_text(encoding="utf-8")
            self.assertIn("/usr/local/bin/openheron", content)
            self.assertIn("<string>gateway</string>", content)
            lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
            self.assertTrue(any("Gateway service manifest written:" in line for line in lines))

    def test_cmd_gateway_service_install_refuses_existing_manifest_without_force(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            data = Path(tmp) / "data"
            manifest = home / "Library" / "LaunchAgents" / "openheron-gateway.plist"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("existing", encoding="utf-8")
            with patch.object(cli, "detect_service_manager", return_value="launchd"):
                with patch.object(cli.Path, "home", return_value=home):
                    with patch.object(cli, "get_data_dir", return_value=data):
                        with patch("builtins.print") as mocked_print:
                            code = cli._cmd_gateway_service_install(
                                force=False,
                                channels="local",
                                enable=False,
                            )

        self.assertEqual(code, 1)
        lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
        self.assertTrue(any("already exists" in line for line in lines))

    def test_cmd_gateway_service_install_enable_runs_launchctl(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            data = Path(tmp) / "data"
            home.mkdir(parents=True, exist_ok=True)
            data.mkdir(parents=True, exist_ok=True)
            with patch.object(cli, "detect_service_manager", return_value="launchd"):
                with patch.object(cli.Path, "home", return_value=home):
                    with patch.object(cli, "get_data_dir", return_value=data):
                        with patch.object(cli, "get_config_path", return_value=data / "config.json"):
                            with patch.object(cli, "load_config", return_value=cli.default_config()):
                                with patch.object(cli.shutil, "which", return_value="/usr/local/bin/openheron"):
                                    with patch.object(cli.subprocess, "run") as mocked_run:
                                        code = cli._cmd_gateway_service_install(
                                            force=False,
                                            channels="local",
                                            enable=True,
                                        )

        self.assertEqual(code, 0)
        self.assertEqual(mocked_run.call_count, 1)
        self.assertEqual(
            mocked_run.call_args.args[0][:3],
            ["launchctl", "load", "-w"],
        )

    def test_cmd_gateway_service_install_enable_failure_returns_error(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            data = Path(tmp) / "data"
            home.mkdir(parents=True, exist_ok=True)
            data.mkdir(parents=True, exist_ok=True)
            with patch.object(cli, "detect_service_manager", return_value="launchd"):
                with patch.object(cli.Path, "home", return_value=home):
                    with patch.object(cli, "get_data_dir", return_value=data):
                        with patch.object(cli, "get_config_path", return_value=data / "config.json"):
                            with patch.object(cli, "load_config", return_value=cli.default_config()):
                                with patch.object(cli.shutil, "which", return_value="/usr/local/bin/openheron"):
                                    with patch.object(
                                        cli.subprocess,
                                        "run",
                                        side_effect=subprocess.CalledProcessError(returncode=1, cmd=["launchctl"]),
                                    ):
                                        code = cli._cmd_gateway_service_install(
                                            force=False,
                                            channels="local",
                                            enable=True,
                                        )

        self.assertEqual(code, 1)

    def test_cmd_gateway_service_status_json_output(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            manifest = home / ".config" / "systemd" / "user" / "openheron-gateway.service"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("[Unit]\n", encoding="utf-8")
            with patch.object(cli, "detect_service_manager", return_value="systemd"):
                with patch.object(cli.Path, "home", return_value=home):
                    with patch("builtins.print") as mocked_print:
                        code = cli._cmd_gateway_service_status(output_json=True)

        self.assertEqual(code, 0)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertTrue(payload["supported"])
        self.assertEqual(payload["manager"], "systemd")
        self.assertTrue(payload["manifestExists"])

    def test_cmd_install_non_interactive_still_prints_summary_lines(self) -> None:
        from openheron import cli

        with patch.object(cli, "_cmd_install_init_setup", return_value=0):
            with patch.object(cli, "_cmd_doctor", return_value=0):
                with patch.object(cli, "bootstrap_env_from_config"):
                    with patch.object(cli, "_install_summary_lines", return_value=["sx"]):
                        with patch.object(cli, "_install_prereq_lines", return_value=["px"]):
                            with patch("sys.stdin.isatty", return_value=False):
                                with patch("builtins.print") as mocked_print:
                                    code = cli._cmd_install(force=False, non_interactive=False)

        self.assertEqual(code, 0)
        lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
        self.assertIn("sx", lines)
        self.assertIn("px", lines)

    def test_interactive_install_input_falls_back_to_builtin_input(self) -> None:
        from openheron import cli

        original_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "questionary":
                raise ImportError("no questionary")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            with patch("builtins.input", return_value="value"):
                answer = cli._interactive_install_input("Prompt> ")
        self.assertEqual(answer, "value")

    def test_interactive_install_select_falls_back_to_builtin_input(self) -> None:
        from openheron import cli

        original_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "questionary":
                raise ImportError("no questionary")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            with patch("builtins.input", return_value="openai"):
                answer = cli._interactive_install_select("Choose provider", ["google", "openai"], "google")
        self.assertEqual(answer, "openai")

    def test_interactive_install_multi_select_falls_back_to_builtin_input(self) -> None:
        from openheron import cli

        original_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "questionary":
                raise ImportError("no questionary")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            with patch("builtins.input", return_value="local,feishu"):
                answer = cli._interactive_install_multi_select(
                    "Enable channels",
                    ["local", "feishu", "telegram"],
                    ["local"],
                )
                self.assertEqual(answer, ["local", "feishu"])

    def test_can_use_questionary_returns_false_without_tty(self) -> None:
        from openheron import cli

        with patch.object(cli.sys.stdin, "isatty", return_value=False):
            with patch.object(cli.sys.stdout, "isatty", return_value=True):
                self.assertFalse(cli._can_use_questionary())

    def test_interactive_install_select_skips_questionary_when_not_tty(self) -> None:
        from openheron import cli

        with patch.object(cli, "_can_use_questionary", return_value=False):
            with patch("builtins.input", return_value=""):
                answer = cli._interactive_install_select("Choose provider", ["google", "openai"], "google")
        self.assertEqual(answer, "google")

    def test_cmd_install_returns_failure_when_doctor_fails(self) -> None:
        from openheron import cli

        with patch.object(cli, "_cmd_install_init_setup", return_value=0):
            with patch.object(cli, "_cmd_doctor", return_value=1):
                with patch.object(cli, "bootstrap_env_from_config"):
                    with patch("builtins.print"):
                        code = cli._cmd_install(force=False, non_interactive=False)
        self.assertEqual(code, 1)

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
            with patch("openheron.app.cli.shutil.which", return_value="/usr/bin/adk"):
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
        self.assertIn("installPrereqs", payload)
        self.assertIn("heartbeat", payload)
        self.assertIn("multiAgent", payload)
        self.assertIn("routePreview", payload["multiAgent"])

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
            with patch("openheron.app.cli.shutil.which", return_value="/usr/bin/adk"):
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

    def test_cmd_doctor_json_output_includes_heartbeat_snapshot(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            snapshot_path = workspace / ".openheron" / "heartbeat_status.json"
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {"running": True, "recent_reason_counts": {"exec": 1}}
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            fake_registry = pytypes.SimpleNamespace(workspace=workspace, list_skills=lambda: [])
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
                with patch("openheron.app.cli.shutil.which", return_value="/usr/bin/adk"):
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
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertTrue(payload["heartbeat"]["snapshot_available"])
        self.assertEqual(payload["heartbeat"]["status"], snapshot)

    def test_cmd_doctor_json_output_includes_install_prereqs(self) -> None:
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
            with patch("openheron.app.cli.shutil.which", return_value="/usr/bin/adk"):
                with patch.object(cli, "validate_provider_runtime", return_value=None):
                    with patch.object(cli, "get_registry", return_value=fake_registry):
                        with patch.object(cli, "load_session_config", return_value=fake_session_cfg):
                            with patch.object(cli, "parse_enabled_channels", return_value=["local"]):
                                with patch.object(cli, "validate_channel_setup", return_value=[]):
                                    with patch.object(cli, "load_security_policy", return_value=fake_security_policy):
                                        with patch.object(cli, "build_mcp_toolsets_from_env", return_value=[]):
                                            with patch.object(cli, "_install_prereq_lines", return_value=["p1", "p2"]):
                                                with patch.object(cli.logger, "debug"):
                                                    with patch("builtins.print") as mocked_print:
                                                        code = cli._cmd_doctor(output_json=True, verbose=False)

        self.assertEqual(code, 0)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertEqual(payload["installPrereqs"], ["p1", "p2"])
        self.assertIn("summary", payload["fix"])

    def test_doctor_apply_minimal_fixes_updates_config_from_env(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["providers"]["google"]["enabled"] = True
            cfg["providers"]["google"]["apiKey"] = ""
            cfg["channels"]["telegram"]["enabled"] = True
            cfg["channels"]["telegram"]["token"] = ""
            cli.save_config(cfg, config_path=config_path)

            with patch.dict(
                os.environ,
                {
                    "GOOGLE_API_KEY": "env-google-key",
                    "TELEGRAM_BOT_TOKEN": "env-telegram-token",
                },
                clear=False,
            ):
                changes, skipped, failed = cli._doctor_apply_minimal_fixes(config_path)

            updated = cli.load_config(config_path=config_path)

        self.assertIn("providers.google.apiKey <- GOOGLE_API_KEY", changes)
        self.assertIn("channels.telegram.token <- TELEGRAM_BOT_TOKEN", changes)
        self.assertIsInstance(skipped, list)
        self.assertEqual(failed, [])
        self.assertEqual(updated["providers"]["google"]["apiKey"], "env-google-key")
        self.assertEqual(updated["channels"]["telegram"]["token"], "env-telegram-token")

    def test_doctor_apply_minimal_fixes_can_emit_structured_events(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["providers"]["google"]["enabled"] = True
            cfg["providers"]["google"]["apiKey"] = ""
            cli.save_config(cfg, config_path=config_path)

            events: list[dict[str, str]] = []
            with patch.dict(os.environ, {"GOOGLE_API_KEY": "env-google-key"}, clear=False):
                changes, _skipped, failed = cli._doctor_apply_minimal_fixes(config_path, event_sink=events)

        self.assertIn("providers.google.apiKey <- GOOGLE_API_KEY", changes)
        self.assertEqual(failed, [])
        self.assertTrue(events)
        self.assertIn("outcome", events[0])
        self.assertIn("code", events[0])
        self.assertIn("rule", events[0])
        self.assertIn("message", events[0])
        self.assertTrue(
            any(
                event["outcome"] == "applied"
                and event["code"] == "provider.env.api_key_backfilled"
                and event["message"] == "providers.google.apiKey <- GOOGLE_API_KEY"
                for event in events
            )
        )

    def test_doctor_apply_minimal_fixes_e2e_apply_with_mixed_outcomes(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["providers"]["google"]["enabled"] = True
            cfg["providers"]["google"]["apiKey"] = ""
            cfg["channels"]["telegram"]["enabled"] = True
            cfg["channels"]["telegram"]["token"] = ""
            cfg["channels"]["email"]["enabled"] = True
            cfg["channels"]["email"]["consentGranted"] = False
            cli.save_config(cfg, config_path=config_path)

            events: list[dict[str, str]] = []
            with patch.dict(
                os.environ,
                {
                    "GOOGLE_API_KEY": "env-google-key",
                    "EMAIL_CONSENT_GRANTED": "true",
                },
                clear=False,
            ):
                changes, skipped, failed = cli._doctor_apply_minimal_fixes(config_path, event_sink=events)

            updated = cli.load_config(config_path=config_path)

        self.assertIn("providers.google.apiKey <- GOOGLE_API_KEY", changes)
        self.assertIn("channels.email.consentGranted <- EMAIL_CONSENT_GRANTED", changes)
        self.assertIn("TELEGRAM_BOT_TOKEN missing", skipped)
        self.assertEqual(failed, [])
        self.assertEqual(updated["providers"]["google"]["apiKey"], "env-google-key")
        self.assertTrue(updated["channels"]["email"]["consentGranted"])
        self.assertTrue(any(event["outcome"] == "applied" for event in events))
        self.assertTrue(any(event["outcome"] == "skipped" for event in events))
        self.assertTrue(any(event["code"] == "provider.env.api_key_backfilled" for event in events))
        self.assertTrue(any(event["code"] == "email.consent.backfilled" for event in events))
        self.assertTrue(any(event["code"] == "channel.env.source_missing" for event in events))

    def test_doctor_apply_minimal_fixes_e2e_save_failure_emits_failed_event(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["providers"]["google"]["enabled"] = True
            cfg["providers"]["google"]["apiKey"] = ""
            cli.save_config(cfg, config_path=config_path)

            events: list[dict[str, str]] = []
            with patch.dict(os.environ, {"GOOGLE_API_KEY": "env-google-key"}, clear=False):
                with patch.object(cli, "save_config", side_effect=RuntimeError("boom")):
                    changes, _skipped, failed = cli._doctor_apply_minimal_fixes(config_path, event_sink=events)

        self.assertIn("providers.google.apiKey <- GOOGLE_API_KEY", changes)
        self.assertEqual(len(failed), 1)
        self.assertIn("save_config failed: boom", failed[0])
        self.assertTrue(
            any(
                event["outcome"] == "failed"
                and event["code"] == "config.save.failed"
                and "save_config failed: boom" in event["message"]
                for event in events
            )
        )

    def test_doctor_apply_minimal_fixes_enables_default_provider_and_local_channel(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            for name, item in cfg["providers"].items():
                if isinstance(item, dict):
                    item["enabled"] = False
            for name, item in cfg["channels"].items():
                if isinstance(item, dict) and "enabled" in item:
                    item["enabled"] = False
            cli.save_config(cfg, config_path=config_path)

            changes, _skipped, _failed = cli._doctor_apply_minimal_fixes(config_path)
            updated = cli.load_config(config_path=config_path)

        self.assertIn(f"providers.{cli.DEFAULT_PROVIDER}.enabled <- true (doctor default)", changes)
        self.assertIn("channels.local.enabled <- true (doctor default)", changes)
        self.assertTrue(updated["providers"][cli.DEFAULT_PROVIDER]["enabled"])
        self.assertTrue(updated["channels"]["local"]["enabled"])

    def test_doctor_apply_minimal_fixes_migrates_legacy_provider_alias_key(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            raw = {
                "providers": {
                    "openai-codex": {
                        "enabled": True,
                        "apiKey": "legacy-key",
                        "model": "openai-codex/gpt-5.1-codex",
                    }
                },
                "channels": {"local": {"enabled": True}},
            }
            config_path.write_text(json.dumps(raw), encoding="utf-8")

            changes, _skipped, _failed = cli._doctor_apply_minimal_fixes(config_path)
            updated = cli.load_config(config_path=config_path)

        joined = "\n".join(changes)
        self.assertIn("providers.openai_codex.enabled <- providers.openai-codex.enabled", joined)
        self.assertIn("providers.openai_codex.apiKey <- providers.openai-codex.apiKey", joined)
        self.assertTrue(updated["providers"]["openai_codex"]["enabled"])
        self.assertEqual(updated["providers"]["openai_codex"]["apiKey"], "legacy-key")

    def test_doctor_apply_minimal_fixes_reports_provider_enabled_skip_for_alias_source(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            raw = {
                "providers": {
                    "openai-codex": {"enabled": True},
                    "openai_codex": {"enabled": True},
                },
                "channels": {"local": {"enabled": True}},
            }
            config_path.write_text(json.dumps(raw), encoding="utf-8")

            _changes, skipped, _failed = cli._doctor_apply_minimal_fixes(config_path)

        joined_skipped = "\n".join(skipped)
        self.assertIn(
            "providers.openai_codex.enabled kept existing value (source providers.openai-codex.enabled)",
            joined_skipped,
        )

    def test_doctor_apply_minimal_fixes_migrates_legacy_provider_api_key_fields(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            raw = {
                "providers": {
                    "google": {
                        "enabled": True,
                        "api_key": "legacy-google-key",
                        "api_base": "https://example.invalid",
                    }
                },
                "channels": {"local": {"enabled": True}},
            }
            config_path.write_text(json.dumps(raw), encoding="utf-8")

            changes, _skipped, _failed = cli._doctor_apply_minimal_fixes(config_path)
            updated = cli.load_config(config_path=config_path)

        joined = "\n".join(changes)
        self.assertIn("providers.google.apiKey <- providers.google.api_key", joined)
        self.assertIn("providers.google.apiBase <- providers.google.api_base", joined)
        self.assertEqual(updated["providers"]["google"]["apiKey"], "legacy-google-key")
        self.assertEqual(updated["providers"]["google"]["apiBase"], "https://example.invalid")

    def test_doctor_apply_minimal_fixes_migrates_legacy_channel_fields(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            raw = {
                "providers": {"google": {"enabled": True}},
                "channels": {
                    "local": {"enabled": True},
                    "telegram": {"enabled": True, "bot_token": "legacy-telegram-token"},
                    "dingtalk": {"enabled": True, "client_id": "legacy-client-id"},
                },
            }
            config_path.write_text(json.dumps(raw), encoding="utf-8")

            changes, _skipped, _failed = cli._doctor_apply_minimal_fixes(config_path)
            updated = cli.load_config(config_path=config_path)

        joined = "\n".join(changes)
        self.assertIn("channels.telegram.token <- channels.telegram.bot_token", joined)
        self.assertIn("channels.dingtalk.clientId <- channels.dingtalk.client_id", joined)
        self.assertEqual(updated["channels"]["telegram"]["token"], "legacy-telegram-token")
        self.assertEqual(updated["channels"]["dingtalk"]["clientId"], "legacy-client-id")

    def test_doctor_fix_mapping_tables_include_core_legacy_cases(self) -> None:
        from openheron import cli

        self.assertIn(("api_key", "apiKey"), cli.LEGACY_PROVIDER_FIELD_MIGRATIONS)
        self.assertIn(("api_base", "apiBase"), cli.LEGACY_PROVIDER_FIELD_MIGRATIONS)
        self.assertIn(("telegram", "bot_token", "token"), cli.LEGACY_CHANNEL_FIELD_MIGRATIONS)
        self.assertIn(("dingtalk", "client_id", "clientId"), cli.LEGACY_CHANNEL_FIELD_MIGRATIONS)
        self.assertIn(("telegram", "token", "TELEGRAM_BOT_TOKEN"), cli.CHANNEL_ENV_BACKFILL_MAPPINGS)

    def test_doctor_ensure_active_provider_enables_default(self) -> None:
        from openheron import cli

        providers_cfg = cli.default_config()["providers"]
        for item in providers_cfg.values():
            if isinstance(item, dict):
                item["enabled"] = False
        changes: list[str] = []

        active = cli._doctor_ensure_active_provider(providers_cfg=providers_cfg, changes=changes)

        self.assertEqual(active, cli.DEFAULT_PROVIDER)
        self.assertTrue(providers_cfg[cli.DEFAULT_PROVIDER]["enabled"])
        self.assertIn(f"providers.{cli.DEFAULT_PROVIDER}.enabled <- true (doctor default)", changes)

    def test_doctor_ensure_at_least_one_enabled_channel_enables_local(self) -> None:
        from openheron import cli

        channels_cfg = cli.default_config()["channels"]
        for item in channels_cfg.values():
            if isinstance(item, dict) and "enabled" in item:
                item["enabled"] = False
        changes: list[str] = []

        cli._doctor_ensure_at_least_one_enabled_channel(channels_cfg=channels_cfg, changes=changes)

        self.assertTrue(channels_cfg["local"]["enabled"])
        self.assertIn("channels.local.enabled <- true (doctor default)", changes)

    def test_doctor_backfill_provider_api_key_from_env_sets_value(self) -> None:
        from openheron import cli

        providers_cfg = cli.default_config()["providers"]
        providers_cfg["google"]["enabled"] = True
        providers_cfg["google"]["apiKey"] = ""
        changes: list[str] = []

        with patch.dict(os.environ, {"GOOGLE_API_KEY": "env-google-key"}, clear=False):
            cli._doctor_backfill_provider_api_key_from_env(
                providers_cfg=providers_cfg,
                active_provider="google",
                changes=changes,
            )

        self.assertEqual(providers_cfg["google"]["apiKey"], "env-google-key")
        self.assertIn("providers.google.apiKey <- GOOGLE_API_KEY", changes)

    def test_doctor_backfill_provider_api_key_from_env_skips_without_active_provider(self) -> None:
        from openheron import cli

        providers_cfg = cli.default_config()["providers"]
        changes: list[str] = []

        with patch.dict(os.environ, {"GOOGLE_API_KEY": "env-google-key"}, clear=False):
            cli._doctor_backfill_provider_api_key_from_env(
                providers_cfg=providers_cfg,
                active_provider=None,
                changes=changes,
            )

        self.assertEqual(changes, [])

    def test_doctor_apply_minimal_fixes_dry_run_does_not_persist(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            raw = {
                "providers": {"google": {"enabled": True, "apiKey": ""}},
                "channels": {"local": {"enabled": True}, "telegram": {"enabled": True, "token": ""}},
            }
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            before = json.loads(config_path.read_text(encoding="utf-8"))

            with patch.dict(
                os.environ,
                {
                    "GOOGLE_API_KEY": "env-google-key",
                    "TELEGRAM_BOT_TOKEN": "env-telegram-token",
                },
                clear=False,
            ):
                changes, _skipped, failed = cli._doctor_apply_minimal_fixes(config_path, dry_run=True)

            after = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertTrue(changes)
        self.assertEqual(failed, [])
        self.assertEqual(before, after)

    def test_doctor_apply_minimal_fixes_reports_channel_skip_reason_when_disabled(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["providers"]["google"]["enabled"] = True
            cfg["channels"]["telegram"]["enabled"] = False
            cfg["channels"]["telegram"]["token"] = ""
            cli.save_config(cfg, config_path=config_path)

            _changes, skipped, failed = cli._doctor_apply_minimal_fixes(config_path)

        self.assertIn("channels.telegram.token skipped (channel disabled)", skipped)
        self.assertEqual(failed, [])

    def test_doctor_apply_minimal_fixes_backfills_email_consent_from_env(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["providers"]["google"]["enabled"] = True
            cfg["channels"]["email"]["enabled"] = True
            cfg["channels"]["email"]["consentGranted"] = False
            cli.save_config(cfg, config_path=config_path)

            with patch.dict(os.environ, {"EMAIL_CONSENT_GRANTED": "true"}, clear=False):
                changes, skipped, failed = cli._doctor_apply_minimal_fixes(config_path)

            updated = cli.load_config(config_path=config_path)

        self.assertIn("channels.email.consentGranted <- EMAIL_CONSENT_GRANTED", changes)
        self.assertTrue(updated["channels"]["email"]["consentGranted"])
        self.assertEqual(failed, [])
        self.assertNotIn("EMAIL_CONSENT_GRANTED missing", skipped)

    def test_doctor_apply_minimal_fixes_reports_non_truthy_email_consent_env(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            cfg = cli.default_config()
            cfg["providers"]["google"]["enabled"] = True
            cfg["channels"]["email"]["enabled"] = True
            cfg["channels"]["email"]["consentGranted"] = False
            cli.save_config(cfg, config_path=config_path)

            with patch.dict(os.environ, {"EMAIL_CONSENT_GRANTED": "nope"}, clear=False):
                _changes, skipped, failed = cli._doctor_apply_minimal_fixes(config_path)

        self.assertIn("EMAIL_CONSENT_GRANTED present but not truthy", skipped)
        self.assertEqual(failed, [])

    def test_doctor_fix_summary_groups_changes(self) -> None:
        from openheron import cli

        summary = cli._doctor_fix_summary(
            [
                "providers.google.enabled <- true (doctor default)",
                "providers.google.apiKey <- GOOGLE_API_KEY",
                "providers.openai_codex.apiKey <- providers.openai-codex.apiKey",
                "custom <- value",
            ],
            ["channels.telegram.token already set"],
            ["save_config failed: boom"],
        )

        self.assertEqual(summary["applied"], 4)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["counts"]["defaults"], 1)
        self.assertEqual(summary["counts"]["env_backfill"], 1)
        self.assertEqual(summary["counts"]["legacy_migration"], 1)
        self.assertEqual(summary["counts"]["other"], 1)
        self.assertEqual(summary["skippedItems"], ["channels.telegram.token already set"])
        self.assertEqual(summary["failedItems"], ["save_config failed: boom"])
        self.assertEqual(summary["reasonCodes"], {})
        self.assertEqual(summary["byRule"], {})

    def test_doctor_fix_summary_includes_reason_codes_and_by_rule(self) -> None:
        from openheron import cli

        summary = cli._doctor_fix_summary(
            ["providers.google.apiKey <- GOOGLE_API_KEY"],
            ["TELEGRAM_BOT_TOKEN missing"],
            [],
            events=[
                {
                    "outcome": "applied",
                    "code": "provider.env.api_key_backfilled",
                    "rule": "provider_env_backfill",
                    "message": "providers.google.apiKey <- GOOGLE_API_KEY",
                },
                {
                    "outcome": "skipped",
                    "code": "channel.env.source_missing",
                    "rule": "channel_env_backfill",
                    "message": "TELEGRAM_BOT_TOKEN missing",
                },
            ],
        )

        self.assertEqual(summary["reasonCodes"]["provider.env.api_key_backfilled"], 1)
        self.assertEqual(summary["reasonCodes"]["channel.env.source_missing"], 1)
        self.assertEqual(summary["byRule"]["provider_env_backfill"]["applied"], 1)
        self.assertEqual(summary["byRule"]["provider_env_backfill"]["total"], 1)
        self.assertEqual(summary["byRule"]["channel_env_backfill"]["skipped"], 1)
        self.assertEqual(summary["byRule"]["channel_env_backfill"]["total"], 1)

    def test_cmd_doctor_json_fix_contains_reason_codes_and_by_rule(self) -> None:
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
            with patch("openheron.app.cli.shutil.which", return_value="/usr/bin/adk"):
                with patch.object(cli, "validate_provider_runtime", return_value=None):
                    with patch.object(cli, "get_registry", return_value=fake_registry):
                        with patch.object(cli, "load_session_config", return_value=fake_session_cfg):
                            with patch.object(cli, "parse_enabled_channels", return_value=["local"]):
                                with patch.object(cli, "validate_channel_setup", return_value=[]):
                                    with patch.object(cli, "load_security_policy", return_value=fake_security_policy):
                                        with patch.object(cli, "build_mcp_toolsets_from_env", return_value=[]):
                                            with patch.object(
                                                cli,
                                                "_doctor_apply_minimal_fixes",
                                                return_value=(
                                                    ["providers.google.apiKey <- GOOGLE_API_KEY"],
                                                    ["TELEGRAM_BOT_TOKEN missing"],
                                                    [],
                                                ),
                                            ):
                                                with patch("builtins.print") as mocked_print:
                                                    code = cli._cmd_doctor(
                                                        output_json=True,
                                                        verbose=False,
                                                        fix=True,
                                                        fix_dry_run=False,
                                                    )
        self.assertEqual(code, 0)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertIn("reasonCodes", payload["fix"])
        self.assertIn("byRule", payload["fix"])
        self.assertIsInstance(payload["fix"]["reasonCodes"], dict)
        self.assertIsInstance(payload["fix"]["byRule"], dict)

    def test_cmd_doctor_text_output_includes_heartbeat_summary(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            snapshot_path = workspace / ".openheron" / "heartbeat_status.json"
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_text(
                json.dumps({"last_status": "ran", "last_reason": "exec:foreground", "recent_reason_counts": {"exec": 2}}),
                encoding="utf-8",
            )
            fake_registry = pytypes.SimpleNamespace(workspace=workspace, list_skills=lambda: [])
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
                with patch("openheron.app.cli.shutil.which", return_value="/usr/bin/adk"):
                    with patch.object(cli, "validate_provider_runtime", return_value=None):
                        with patch.object(cli, "get_registry", return_value=fake_registry):
                            with patch.object(cli, "load_session_config", return_value=fake_session_cfg):
                                with patch.object(cli, "parse_enabled_channels", return_value=["local"]):
                                    with patch.object(cli, "validate_channel_setup", return_value=[]):
                                        with patch.object(cli, "load_security_policy", return_value=fake_security_policy):
                                            with patch.object(cli, "build_mcp_toolsets_from_env", return_value=[]):
                                                with patch.object(cli.logger, "debug"):
                                                    with patch("builtins.print") as mocked_print:
                                                        code = cli._cmd_doctor(output_json=False, verbose=False)

        self.assertEqual(code, 0)
        lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
        self.assertTrue(any("Heartbeat: last_status=ran, last_reason=exec:foreground" in line for line in lines))
        self.assertTrue(any("GUI runtime: mode=builtin_only" in line for line in lines))
        self.assertTrue(any("Environment looks good." in line for line in lines))

    def test_cmd_doctor_text_output_includes_install_prereqs(self) -> None:
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
            with patch("openheron.app.cli.shutil.which", return_value="/usr/bin/adk"):
                with patch.object(cli, "validate_provider_runtime", return_value=None):
                    with patch.object(cli, "get_registry", return_value=fake_registry):
                        with patch.object(cli, "load_session_config", return_value=fake_session_cfg):
                            with patch.object(cli, "parse_enabled_channels", return_value=["local"]):
                                with patch.object(cli, "validate_channel_setup", return_value=[]):
                                    with patch.object(cli, "load_security_policy", return_value=fake_security_policy):
                                        with patch.object(cli, "build_mcp_toolsets_from_env", return_value=[]):
                                            with patch.object(
                                                cli,
                                                "_install_prereq_lines",
                                                return_value=["x1", "optional package rich missing"],
                                            ):
                                                with patch.object(cli.logger, "debug"):
                                                    with patch("builtins.print") as mocked_print:
                                                        code = cli._cmd_doctor(output_json=False, verbose=False)

        self.assertEqual(code, 0)
        lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
        self.assertTrue(any("Install prereq [ok]: x1" == line for line in lines))
        self.assertTrue(any("Install prereq [warn]: optional package rich missing" == line for line in lines))

    def test_cmd_doctor_text_output_hints_multi_agent_conflicts(self) -> None:
        from openheron import cli

        fake_registry = pytypes.SimpleNamespace(workspace=Path("/tmp"), list_skills=lambda: [])
        fake_session_cfg = pytypes.SimpleNamespace(db_url="sqlite+aiosqlite:////tmp/sessions.db")
        fake_security_policy = pytypes.SimpleNamespace(
            restrict_to_workspace=False,
            allow_exec=True,
            allow_network=True,
            exec_allowlist=(),
        )
        cfg = cli.default_config()
        cfg["agents"]["list"] = [
            {"id": "main", "default": True},
            {"id": "biz"},
        ]
        cfg["bindings"] = [
            {"agentId": "main", "match": {"channel": "whatsapp", "accountId": "business"}},
            {"agentId": "biz", "match": {"channel": "whatsapp", "accountId": "business"}},
        ]
        with patch.dict(
            os.environ,
            {
                "OPENHERON_PROVIDER": "google",
                "OPENHERON_PROVIDER_ENABLED": "1",
                "GOOGLE_API_KEY": "k",
            },
            clear=False,
        ):
            with patch("openheron.app.cli.shutil.which", return_value="/usr/bin/adk"):
                with patch.object(cli, "validate_provider_runtime", return_value=None):
                    with patch.object(cli, "load_config", return_value=cfg):
                        with patch.object(cli, "get_registry", return_value=fake_registry):
                            with patch.object(cli, "load_session_config", return_value=fake_session_cfg):
                                with patch.object(cli, "parse_enabled_channels", return_value=["local"]):
                                    with patch.object(cli, "validate_channel_setup", return_value=[]):
                                        with patch.object(cli, "load_security_policy", return_value=fake_security_policy):
                                            with patch.object(cli, "build_mcp_toolsets_from_env", return_value=[]):
                                                with patch.object(cli.logger, "debug"):
                                                    with patch("builtins.print") as mocked_print:
                                                        code = cli._cmd_doctor(output_json=False, verbose=False)

        self.assertEqual(code, 0)
        lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
        self.assertTrue(any("Multi-agent routing conflicts detected: 1." in line for line in lines))
        self.assertTrue(any("multiAgent.summary.conflicts" in line for line in lines))

    def test_cmd_routes_lint_json_output_contains_summary_and_preview(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        cfg["agents"]["list"] = [{"id": "main", "default": True}, {"id": "biz"}]
        cfg["bindings"] = [{"agentId": "biz", "match": {"channel": "whatsapp", "accountId": "business"}}]
        with patch.object(cli, "load_config", return_value=cfg):
            with patch("builtins.print") as mocked_print:
                code = cli._cmd_routes_lint(output_json=True, limit=3)
        self.assertEqual(code, 0)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertTrue(payload["ok"])
        self.assertIn("summary", payload)
        self.assertIn("routePreview", payload)
        self.assertIn("suggestions", payload)
        self.assertIn("warnings", payload)
        self.assertEqual(payload["suggestions"], [])
        self.assertEqual(payload["warnings"], [])
        self.assertLessEqual(len(payload["routePreview"]), 3)

    def test_cmd_routes_lint_json_output_suggests_scope_fix_for_invalid_roles(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        cfg["agents"]["list"] = [{"id": "main", "default": True}]
        cfg["bindings"] = [
            {"agentId": "main", "match": {"channel": "discord", "roles": ["admin"]}},
        ]
        with patch.object(cli, "load_config", return_value=cfg):
            with patch("builtins.print") as mocked_print:
                code = cli._cmd_routes_lint(output_json=True, limit=3)
        self.assertEqual(code, 1)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertTrue(any("match.roles" in item for item in payload["issues"]))
        self.assertTrue(any("match.guild.id" in item for item in payload["suggestions"]))

    def test_cmd_routes_lint_json_output_warns_scope_reachability_by_channel(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        cfg["agents"]["list"] = [{"id": "main", "default": True}]
        cfg["bindings"] = [
            {"agentId": "main", "match": {"channel": "telegram", "guild": {"id": "g1"}}},
            {"agentId": "main", "match": {"channel": "discord", "guild": {"id": "g1"}}},
        ]
        with patch.object(cli, "load_config", return_value=cfg):
            with patch("builtins.print") as mocked_print:
                code = cli._cmd_routes_lint(output_json=True, limit=3)
        self.assertEqual(code, 0)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertEqual(len(payload["warnings"]), 1)
        self.assertIn("channel 'telegram'", payload["warnings"][0])
        self.assertTrue(any("scope matching is required" in item for item in payload["suggestions"]))

    def test_cmd_routes_lint_text_output_reports_conflicts(self) -> None:
        from openheron import cli

        cfg = cli.default_config()
        cfg["agents"]["list"] = [{"id": "main", "default": True}, {"id": "biz"}]
        cfg["bindings"] = [
            {"agentId": "main", "match": {"channel": "whatsapp", "accountId": "business"}},
            {"agentId": "biz", "match": {"channel": "whatsapp", "accountId": "business"}},
        ]
        with patch.object(cli, "load_config", return_value=cfg):
            with patch("builtins.print") as mocked_print:
                code = cli._cmd_routes_lint(output_json=False, limit=4)
        self.assertEqual(code, 1)
        lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
        self.assertTrue(any(line.startswith("Routes lint:") for line in lines))
        self.assertTrue(any(line == "Routing conflicts:" for line in lines))
        self.assertTrue(any(line == "Suggested actions:" for line in lines))
        self.assertTrue(any("keep exactly one target agent" in line for line in lines))

    def test_cmd_routes_stats_json_output_contains_counts(self) -> None:
        from openheron import cli

        snapshot = {
            "generatedAt": "2026-02-27T00:00:00+00:00",
            "totalMessages": 3,
            "byAgent": {"main": 2, "biz": 1},
            "byChannel": {"whatsapp": 3},
            "byMatchedBy": {"binding.account": 3},
            "recent": [{"agentId": "main"}, {"agentId": "biz"}, {"agentId": "main"}],
        }
        fake_registry = pytypes.SimpleNamespace(workspace=Path("/tmp"), list_skills=lambda: [])
        with patch.object(cli, "get_registry", return_value=fake_registry):
            with patch.object(cli, "read_route_stats_snapshot", return_value=snapshot):
                with patch("builtins.print") as mocked_print:
                    code = cli._cmd_routes_stats(output_json=True, limit=2)
        self.assertEqual(code, 0)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stats"]["totalMessages"], 3)
        self.assertEqual(len(payload["stats"]["recent"]), 2)

    def test_cmd_routes_stats_json_output_applies_window_hours(self) -> None:
        from openheron import cli

        now = dt.datetime.now(dt.timezone.utc)
        snapshot = {
            "generatedAt": now.isoformat(),
            "totalMessages": 4,
            "recent": [
                {
                    "at": (now - dt.timedelta(hours=10)).isoformat(),
                    "agentId": "main",
                    "channel": "whatsapp",
                    "matchedBy": "binding.account",
                },
                {
                    "at": (now - dt.timedelta(minutes=30)).isoformat(),
                    "agentId": "biz",
                    "channel": "whatsapp",
                    "matchedBy": "binding.peer",
                },
            ],
        }
        fake_registry = pytypes.SimpleNamespace(workspace=Path("/tmp"), list_skills=lambda: [])
        with patch.object(cli, "get_registry", return_value=fake_registry):
            with patch.object(cli, "read_route_stats_snapshot", return_value=snapshot):
                with patch("builtins.print") as mocked_print:
                    code = cli._cmd_routes_stats(output_json=True, limit=10, window_hours=1)
        self.assertEqual(code, 0)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertEqual(payload["stats"]["windowHours"], 1)
        self.assertEqual(payload["stats"]["totalMessagesInWindow"], 1)
        self.assertEqual(payload["stats"]["byAgent"], {"biz": 1})

    def test_cmd_routes_stats_text_output_includes_top_rankings(self) -> None:
        from openheron import cli

        snapshot = {
            "generatedAt": "2026-02-27T00:00:00+00:00",
            "totalMessages": 5,
            "recent": [
                {"agentId": "main", "channel": "whatsapp", "matchedBy": "binding.peer"},
                {"agentId": "main", "channel": "whatsapp", "matchedBy": "binding.peer"},
                {"agentId": "biz", "channel": "whatsapp", "matchedBy": "binding.account"},
                {"agentId": "main", "channel": "telegram", "matchedBy": "binding.account"},
                {"agentId": "biz", "channel": "whatsapp", "matchedBy": "binding.peer"},
            ],
        }
        fake_registry = pytypes.SimpleNamespace(workspace=Path("/tmp"), list_skills=lambda: [])
        with patch.object(cli, "get_registry", return_value=fake_registry):
            with patch.object(cli, "read_route_stats_snapshot", return_value=snapshot):
                with patch("builtins.print") as mocked_print:
                    code = cli._cmd_routes_stats(output_json=False, limit=2)
        self.assertEqual(code, 0)
        lines = [call.args[0] for call in mocked_print.call_args_list if call.args]
        self.assertTrue(any(line.startswith("Routes stats:") for line in lines))
        self.assertIn("Top agents: main:3, biz:2", lines)
        self.assertIn("Top channels: whatsapp:4, telegram:1", lines)
        self.assertIn("Top matchedBy: binding.peer:3, binding.account:2", lines)
        self.assertIn("Recent samples:", lines)
        sample_lines = [line for line in lines if line.startswith("- at=")]
        self.assertEqual(len(sample_lines), 2)
        self.assertTrue(any("agent=main" in line and "channel=telegram" in line for line in sample_lines))
        self.assertTrue(any("agent=biz" in line and "channel=whatsapp" in line for line in sample_lines))

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
            with patch("builtins.print") as mocked_info:
                cli._log_mcp_startup_summary([])
        mocked_info.assert_called_once_with("MCP toolsets: none configured")

        with patch.object(
            cli,
            "summarize_mcp_toolsets",
            return_value=[{"name": "filesystem", "transport": "stdio", "prefix": "mcp_filesystem_"}],
        ):
            with patch("builtins.print") as mocked_info:
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
                    with patch("builtins.print") as mocked_info:
                        code = cli._cmd_mcps()

        self.assertEqual(code, 0)
        info_text = "\n".join(call.args[0] for call in mocked_info.call_args_list if call.args)
        self.assertIn("Connected MCP servers: 1", info_text)
        self.assertIn("- filesystem (stdio) | APIs: 2", info_text)
        self.assertIn("  - read_file: Read file content", info_text)
        self.assertIn("read_file", info_text)
        self.assertIn("Read file content", info_text)
        self.assertNotIn("输入:", info_text)
        self.assertNotIn("输出:", info_text)
        self.assertNotIn("- bad_remote (http)", info_text)

    def test_cmd_mcps_closes_toolsets_in_same_run(self) -> None:
        from openheron import cli

        fake_toolset = pytypes.SimpleNamespace(
            meta=pytypes.SimpleNamespace(name="filesystem"),
            close=AsyncMock(),
        )
        fake_results = [
            {"name": "filesystem", "transport": "stdio", "status": "ok"},
        ]
        with patch.object(cli, "build_mcp_toolsets_from_env", return_value=[fake_toolset]):
            with patch.object(cli, "probe_mcp_toolsets", new=AsyncMock(return_value=fake_results)):
                with patch.object(
                    cli,
                    "_collect_connected_mcp_apis",
                    new=AsyncMock(return_value={"filesystem": []}),
                ):
                    with patch("builtins.print"):
                        code = cli._cmd_mcps()

        self.assertEqual(code, 0)
        fake_toolset.close.assert_awaited_once()

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
                with patch("builtins.print") as mocked_info:
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
        from openheron.core.mcp_registry import build_mcp_toolsets

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
                "openheron.app.agent": fake_agent_module,
                "openheron.app.gateway": fake_gateway_module,
            },
        ):
            with patch.object(cli, "parse_enabled_channels", return_value=["local"]):
                with patch.object(cli, "validate_channel_setup", return_value=[]):
                    with patch.object(
                        cli,
                        "_required_mcp_preflight",
                        new=AsyncMock(return_value=["required MCP failed health check (error/transient)"]),
                    ):
                        with patch("builtins.print") as mocked_warning:
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
                "openheron.app.agent": fake_agent_module,
                "openheron.app.gateway": fake_gateway_module,
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
                            with patch("builtins.print") as mocked_info:
                                code = cli._cmd_gateway(
                                    channels="whatsapp",
                                    sender_id="u1",
                                    chat_id="c1",
                                    interactive_local=False,
                                )

        self.assertEqual(code, 1)
        messages = [call.args[0] for call in mocked_info.call_args_list if call.args]
        self.assertIn("[doctor] WhatsApp bridge precheck failed", messages)

    def test_cmd_gateway_start_writes_pid_and_meta(self) -> None:
        from openheron import cli

        fake_proc = pytypes.SimpleNamespace(pid=34567)
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / ".openheron"
            with patch.object(cli, "get_data_dir", return_value=data_dir):
                with patch.object(cli, "parse_enabled_channels", return_value=["local", "feishu"]):
                    with patch("subprocess.Popen", return_value=fake_proc):
                        with patch("builtins.print"):
                            code = cli._cmd_gateway_start(channels="local,feishu", sender_id="u1", chat_id="c1")

            self.assertEqual(code, 0)
            pid_path = data_dir / "log" / "gateway.pid"
            meta_path = data_dir / "log" / "gateway.meta.json"
            self.assertTrue(pid_path.exists())
            self.assertTrue(meta_path.exists())
            self.assertEqual(pid_path.read_text(encoding="utf-8").strip(), "34567")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["pid"], 34567)
            self.assertEqual(meta["channels"], "local,feishu")

    def test_cmd_gateway_stop_cleans_stale_pid(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / ".openheron"
            log_dir = data_dir / "log"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "gateway.pid").write_text("98765\n", encoding="utf-8")
            (log_dir / "gateway.meta.json").write_text('{"pid":98765}\n', encoding="utf-8")
            with patch.object(cli, "get_data_dir", return_value=data_dir):
                with patch.object(cli, "_is_pid_running", return_value=False):
                    with patch("builtins.print"):
                        code = cli._cmd_gateway_stop()

            self.assertEqual(code, 0)
            self.assertFalse((log_dir / "gateway.pid").exists())
            self.assertFalse((log_dir / "gateway.meta.json").exists())

    def test_cmd_install_init_setup_creates_config_and_workspace(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}, clear=False):
                code = cli._cmd_install_init_setup(force=False)

            self.assertEqual(code, 0)
            config_path = Path(tmp) / ".openheron" / "config.json"
            runtime_config_path = Path(tmp) / ".openheron" / "runtime.json"
            self.assertTrue(config_path.exists())
            self.assertTrue(runtime_config_path.exists())
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

        with patch.dict("sys.modules", {"openheron.app.agent": fake_agent_module}):
            with patch("openheron.app.cli.create_runner", return_value=(_FakeRunner(), object())):
                with patch("builtins.print") as mocked_info:
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

        with patch.dict("sys.modules", {"openheron.app.agent": fake_agent_module}):
            with patch("openheron.app.cli.create_runner", return_value=(_FakeRunner(), object())):
                with patch("builtins.print") as mocked_info:
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

    def test_heartbeat_status_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_heartbeat_status", return_value=0) as mocked_status:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["heartbeat", "status", "--json"])
                self.assertEqual(ctx.exception.code, 0)
                mocked_status.assert_called_once_with(output_json=True)
                mocked_bootstrap.assert_called_once()

    def test_token_stats_mode_dispatch(self) -> None:
        from openheron import cli

        with patch.object(cli, "bootstrap_env_from_config") as mocked_bootstrap:
            with patch.object(cli, "_cmd_token_stats", return_value=0) as mocked_stats:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(
                        [
                            "token",
                            "stats",
                            "--json",
                            "--limit",
                            "5",
                            "--provider",
                            "google",
                            "--since",
                            "2026-02-26T00:00:00+08:00",
                            "--until",
                            "2026-02-26T23:59:59+08:00",
                        ]
                    )
                self.assertEqual(ctx.exception.code, 0)
                mocked_stats.assert_called_once_with(
                    output_json=True,
                    limit=5,
                    provider="google",
                    since="2026-02-26T00:00:00+08:00",
                    until="2026-02-26T23:59:59+08:00",
                    last_hours=None,
                )
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

        with patch("builtins.print") as mocked_info:
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
                with patch("builtins.print") as mocked_info:
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
                with patch("builtins.print") as mocked_info:
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
            with patch("builtins.print") as mocked_info:
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
                code = cli._cmd_cron_list(include_disabled=True)

        self.assertEqual(code, 0)
        self.assertEqual(mocked_print.call_count, 2)
        mocked_print.assert_any_call("Scheduled jobs:")
        mocked_print.assert_any_call("- demo (id: j1, every:30s, enabled, next=-)")

    def test_cmd_heartbeat_status_prints_runtime_fields(self) -> None:
        from openheron import cli

        with tempfile.TemporaryDirectory() as tmp:
            snapshot = {
                "running": True,
                "enabled": True,
                "last_status": "ran",
                "last_reason": "cron:job1",
                "target_mode": "last",
                "last_delivery": {"kind": "alert"},
                "recent_reason_counts": {"cron": 2, "exec": 1},
            }
            store = Path(tmp) / ".openheron" / "heartbeat_status.json"
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(json.dumps(snapshot), encoding="utf-8")
            policy = pytypes.SimpleNamespace(workspace_root=Path(tmp))
            with patch.object(cli, "load_security_policy", return_value=policy):
                with patch("builtins.print") as mocked_info:
                    code = cli._cmd_heartbeat_status(output_json=False)

        self.assertEqual(code, 0)
        lines = [call.args[0] for call in mocked_info.call_args_list if call.args]
        self.assertTrue(any("running=True" in line for line in lines))
        self.assertTrue(any("last_reason=cron:job1" in line for line in lines))
        self.assertTrue(any('"cron": 2' in line for line in lines))

    def test_cmd_token_stats_prints_summary_and_recent(self) -> None:
        from openheron import cli

        fake_stats = {
            "requests": 2,
            "request_tokens": 30,
            "response_tokens": 12,
            "request_text_tokens": 28,
            "response_text_tokens": 12,
            "request_image_tokens": 2,
            "response_image_tokens": 0,
            "total_tokens": 42,
            "recent": [
                {
                    "response_at": "2026-02-26T10:00:01+00:00",
                    "provider": "google",
                    "model": "gemini-2.5-pro",
                    "session_id": "s1",
                    "invocation_id": "inv1",
                    "request_tokens": 20,
                    "response_tokens": 10,
                    "request_text_tokens": 18,
                    "response_text_tokens": 10,
                    "request_image_tokens": 2,
                    "response_image_tokens": 0,
                    "total_tokens": 30,
                }
            ],
        }

        with patch.object(cli, "read_token_usage_stats", return_value=fake_stats) as mocked_read:
            with patch.object(cli, "token_usage_db_path", return_value=Path("/tmp/token_usage.db")):
                with patch("builtins.print") as mocked_info:
                    code = cli._cmd_token_stats(
                        output_json=False,
                        limit=10,
                        provider=None,
                        since=None,
                        until=None,
                        last_hours=24,
                    )

        self.assertEqual(code, 0)
        mocked_read.assert_called_once()
        kwargs = mocked_read.call_args.kwargs
        self.assertEqual(kwargs["limit"], 10)
        self.assertIsNone(kwargs["provider"])
        self.assertIsInstance(kwargs["since_ms"], int)
        self.assertIsInstance(kwargs["until_ms"], int)
        self.assertLess(kwargs["since_ms"], kwargs["until_ms"])
        lines = [call.args[0] for call in mocked_info.call_args_list if call.args]
        self.assertTrue(any("requests=2" in line for line in lines))
        self.assertTrue(any("Token DB: /tmp/token_usage.db" in line for line in lines))
        self.assertTrue(any("provider=google" in line for line in lines))
        self.assertTrue(any("last_hours=24" in line for line in lines))

    def test_cmd_token_stats_outputs_json(self) -> None:
        from openheron import cli

        fake_stats = {
            "requests": 0,
            "request_tokens": 0,
            "response_tokens": 0,
            "request_text_tokens": 0,
            "response_text_tokens": 0,
            "request_image_tokens": 0,
            "response_image_tokens": 0,
            "total_tokens": 0,
            "recent": [],
        }
        with patch.object(cli, "read_token_usage_stats", return_value=fake_stats):
            with patch.object(cli, "token_usage_db_path", return_value=Path("/tmp/token_usage.db")):
                with patch("builtins.print") as mocked_info:
                    code = cli._cmd_token_stats(
                        output_json=True,
                        limit=20,
                        provider="google",
                        since="2026-02-26T00:00:00+08:00",
                        until="2026-02-26T23:59:59+08:00",
                        last_hours=None,
                    )

        self.assertEqual(code, 0)
        payload = json.loads(mocked_info.call_args[0][0])
        self.assertEqual(payload["provider"], "google")
        self.assertEqual(payload["requests"], 0)
        self.assertEqual(payload["dbPath"], "/tmp/token_usage.db")
        self.assertEqual(payload["since"], "2026-02-26T00:00:00+08:00")
        self.assertEqual(payload["until"], "2026-02-26T23:59:59+08:00")

    def test_cmd_token_stats_invalid_time_range_returns_error(self) -> None:
        from openheron import cli

        with patch("builtins.print") as mocked_info:
            code = cli._cmd_token_stats(
                output_json=False,
                limit=20,
                provider=None,
                since="2026-02-27T00:00:00+08:00",
                until="2026-02-26T23:59:59+08:00",
                last_hours=None,
            )
        self.assertEqual(code, 1)
        self.assertIn("--since must be earlier", mocked_info.call_args[0][0])

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
            with patch("openheron.app.cli.shutil.which", return_value="/usr/bin/adk"):
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

        with patch("builtins.print") as mocked_info:
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
                with patch("openheron.app.cli.subprocess.run") as mocked_run:
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
                            with patch("openheron.app.cli.subprocess.Popen", return_value=fake_proc) as mocked_popen:
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
            with patch("builtins.print") as mocked_info:
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
                    with patch("builtins.print") as mocked_info:
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
                    with patch("builtins.print"):
                        code = cli._cmd_channels_bridge_stop(channel_name="whatsapp")

            self.assertEqual(code, 0)
            self.assertFalse(state_path.exists())


if __name__ == "__main__":
    unittest.main()
