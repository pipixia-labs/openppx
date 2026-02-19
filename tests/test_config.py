"""Tests for persistent config helpers."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from sentientagent_v2.config import (
    apply_config_to_env,
    bootstrap_env_from_config,
    default_config,
    load_config,
    save_config,
)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_load_missing_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp) / "config.json")
        self.assertTrue(cfg["channels"]["local"]["enabled"])
        self.assertFalse(cfg["channels"]["feishu"]["enabled"])
        self.assertIn("telegram", cfg["channels"])
        self.assertIn("whatsapp", cfg["channels"])
        self.assertIn("discord", cfg["channels"])
        self.assertIn("mochat", cfg["channels"])
        self.assertIn("dingtalk", cfg["channels"])
        self.assertIn("email", cfg["channels"])
        self.assertIn("slack", cfg["channels"])
        self.assertIn("qq", cfg["channels"])
        self.assertTrue(cfg["providers"]["google"]["enabled"])
        self.assertTrue(cfg["web"]["search"]["enabled"])
        self.assertEqual(cfg["session"]["dbUrl"], "")
        self.assertFalse(cfg["security"]["restrictToWorkspace"])
        self.assertTrue(cfg["security"]["allowExec"])
        self.assertTrue(cfg["security"]["allowNetwork"])
        self.assertEqual(cfg["security"]["execAllowlist"], [])
        self.assertEqual(cfg["tools"]["mcpServers"], {})

    def test_save_then_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["channels"]["local"]["enabled"] = False
            cfg["channels"]["feishu"]["enabled"] = True
            cfg["channels"]["feishu"]["appId"] = "app-id-1"
            cfg["channels"]["feishu"]["appSecret"] = "app-secret-1"
            save_config(cfg, path)
            loaded = load_config(path)

        self.assertFalse(loaded["channels"]["local"]["enabled"])
        self.assertTrue(loaded["channels"]["feishu"]["enabled"])
        self.assertEqual(loaded["channels"]["feishu"]["appId"], "app-id-1")
        self.assertEqual(loaded["channels"]["feishu"]["appSecret"], "app-secret-1")

    def test_apply_config_to_env_respects_existing_values(self) -> None:
        os.environ["SENTIENTAGENT_V2_MODEL"] = "from-shell"
        os.environ["GOOGLE_API_KEY"] = "key-from-shell"
        cfg = default_config()
        cfg["providers"]["google"]["model"] = "from-config"
        cfg["providers"]["google"]["apiKey"] = "key-from-config"
        apply_config_to_env(cfg, overwrite=False)
        self.assertEqual(os.environ["SENTIENTAGENT_V2_MODEL"], "from-shell")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "key-from-shell")

        apply_config_to_env(cfg, overwrite=True)
        self.assertEqual(os.environ["SENTIENTAGENT_V2_MODEL"], "from-config")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "key-from-config")

    def test_bootstrap_env_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["channels"]["local"]["enabled"] = False
            cfg["channels"]["feishu"]["enabled"] = True
            cfg["channels"]["feishu"]["appId"] = "app-id"
            cfg["channels"]["feishu"]["appSecret"] = "app-secret"
            cfg["channels"]["feishu"]["allowFrom"] = ["ou_1", "ou_2"]
            cfg["channels"]["telegram"]["enabled"] = True
            cfg["channels"]["telegram"]["token"] = "tg-token"
            cfg["channels"]["telegram"]["allowFrom"] = ["u1", "u2"]
            cfg["channels"]["telegram"]["proxy"] = "http://127.0.0.1:7890"
            cfg["channels"]["whatsapp"]["enabled"] = True
            cfg["channels"]["whatsapp"]["bridgeUrl"] = "ws://127.0.0.1:3001"
            cfg["channels"]["whatsapp"]["bridgeToken"] = "wa-bridge-token"
            cfg["channels"]["whatsapp"]["allowFrom"] = ["8613800138000", "8613900139000"]
            cfg["channels"]["discord"]["enabled"] = True
            cfg["channels"]["discord"]["token"] = "discord-token"
            cfg["channels"]["discord"]["allowFrom"] = ["du1", "du2"]
            cfg["channels"]["discord"]["pollChannels"] = ["123", "456"]
            cfg["channels"]["mochat"]["enabled"] = True
            cfg["channels"]["mochat"]["baseUrl"] = "https://mochat.io"
            cfg["channels"]["mochat"]["clawToken"] = "mochat-claw-token"
            cfg["channels"]["mochat"]["agentUserId"] = "agent_u1"
            cfg["channels"]["mochat"]["sessions"] = ["session_1", "session_2"]
            cfg["channels"]["mochat"]["panels"] = ["panel_1", "panel_2"]
            cfg["channels"]["mochat"]["allowFrom"] = ["mo_u1", "mo_u2"]
            cfg["channels"]["mochat"]["pollIntervalSeconds"] = 9
            cfg["channels"]["mochat"]["watchTimeoutMs"] = 12000
            cfg["channels"]["mochat"]["watchLimit"] = 21
            cfg["channels"]["mochat"]["panelLimit"] = 88
            cfg["channels"]["dingtalk"]["enabled"] = True
            cfg["channels"]["dingtalk"]["clientId"] = "dt-app-id"
            cfg["channels"]["dingtalk"]["clientSecret"] = "dt-app-secret"
            cfg["channels"]["dingtalk"]["allowFrom"] = ["dt_u1", "dt_u2"]
            cfg["channels"]["dingtalk"]["streamModeEnabled"] = False
            cfg["channels"]["dingtalk"]["streamReconnectDelaySeconds"] = 13
            cfg["channels"]["email"]["enabled"] = True
            cfg["channels"]["email"]["consentGranted"] = True
            cfg["channels"]["email"]["smtpHost"] = "smtp.example.com"
            cfg["channels"]["email"]["smtpUsername"] = "bot@example.com"
            cfg["channels"]["email"]["smtpPassword"] = "pw"
            cfg["channels"]["email"]["fromAddress"] = "bot@example.com"
            cfg["channels"]["email"]["allowFrom"] = ["a@example.com", "b@example.com"]
            cfg["channels"]["slack"]["enabled"] = True
            cfg["channels"]["slack"]["botToken"] = "xoxb-token"
            cfg["channels"]["slack"]["appToken"] = "xapp-token"
            cfg["channels"]["slack"]["allowFrom"] = ["U1", "U2"]
            cfg["channels"]["slack"]["pollChannels"] = ["C1", "C2"]
            cfg["channels"]["qq"]["enabled"] = True
            cfg["channels"]["qq"]["appId"] = "qq-app-id"
            cfg["channels"]["qq"]["secret"] = "qq-secret"
            cfg["channels"]["qq"]["allowFrom"] = ["qq_u1", "qq_u2"]
            cfg["session"]["dbUrl"] = "sqlite+aiosqlite:////tmp/sessions.db"
            cfg["providers"]["google"]["apiKey"] = "google-key"
            cfg["web"]["search"]["enabled"] = False
            cfg["security"]["restrictToWorkspace"] = True
            cfg["security"]["allowExec"] = False
            cfg["security"]["allowNetwork"] = False
            cfg["security"]["execAllowlist"] = ["python", "ls", "python"]
            save_config(cfg, path)

            os.environ.pop("SENTIENTAGENT_V2_CHANNELS", None)
            os.environ.pop("FEISHU_APP_ID", None)
            os.environ.pop("FEISHU_ALLOW_FROM", None)
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            os.environ.pop("TELEGRAM_ALLOW_FROM", None)
            os.environ.pop("TELEGRAM_PROXY", None)
            os.environ.pop("WHATSAPP_BRIDGE_URL", None)
            os.environ.pop("WHATSAPP_BRIDGE_TOKEN", None)
            os.environ.pop("WHATSAPP_ALLOW_FROM", None)
            os.environ.pop("DISCORD_BOT_TOKEN", None)
            os.environ.pop("DISCORD_ALLOW_FROM", None)
            os.environ.pop("DISCORD_POLL_CHANNELS", None)
            os.environ.pop("MOCHAT_BASE_URL", None)
            os.environ.pop("MOCHAT_CLAW_TOKEN", None)
            os.environ.pop("MOCHAT_AGENT_USER_ID", None)
            os.environ.pop("MOCHAT_SESSIONS", None)
            os.environ.pop("MOCHAT_PANELS", None)
            os.environ.pop("MOCHAT_ALLOW_FROM", None)
            os.environ.pop("MOCHAT_POLL_INTERVAL_SECONDS", None)
            os.environ.pop("MOCHAT_WATCH_TIMEOUT_MS", None)
            os.environ.pop("MOCHAT_WATCH_LIMIT", None)
            os.environ.pop("MOCHAT_PANEL_LIMIT", None)
            os.environ.pop("DINGTALK_CLIENT_ID", None)
            os.environ.pop("DINGTALK_CLIENT_SECRET", None)
            os.environ.pop("DINGTALK_ALLOW_FROM", None)
            os.environ.pop("DINGTALK_STREAM_MODE_ENABLED", None)
            os.environ.pop("DINGTALK_STREAM_RECONNECT_DELAY_SECONDS", None)
            os.environ.pop("EMAIL_CONSENT_GRANTED", None)
            os.environ.pop("EMAIL_SMTP_HOST", None)
            os.environ.pop("EMAIL_SMTP_USERNAME", None)
            os.environ.pop("EMAIL_SMTP_PASSWORD", None)
            os.environ.pop("EMAIL_FROM_ADDRESS", None)
            os.environ.pop("EMAIL_ALLOW_FROM", None)
            os.environ.pop("SLACK_BOT_TOKEN", None)
            os.environ.pop("SLACK_APP_TOKEN", None)
            os.environ.pop("SLACK_ALLOW_FROM", None)
            os.environ.pop("SLACK_POLL_CHANNELS", None)
            os.environ.pop("QQ_APP_ID", None)
            os.environ.pop("QQ_SECRET", None)
            os.environ.pop("QQ_ALLOW_FROM", None)
            os.environ.pop("SENTIENTAGENT_V2_SESSION_DB_URL", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("BRAVE_API_KEY", None)
            os.environ.pop("SENTIENTAGENT_V2_WEB_SEARCH_ENABLED", None)
            os.environ.pop("SENTIENTAGENT_V2_RESTRICT_TO_WORKSPACE", None)
            os.environ.pop("SENTIENTAGENT_V2_ALLOW_EXEC", None)
            os.environ.pop("SENTIENTAGENT_V2_ALLOW_NETWORK", None)
            os.environ.pop("SENTIENTAGENT_V2_EXEC_ALLOWLIST", None)
            loaded = bootstrap_env_from_config(path)

        self.assertIsNotNone(loaded)
        self.assertEqual(os.environ["SENTIENTAGENT_V2_CHANNELS"], "feishu,telegram,whatsapp,discord,mochat,dingtalk,email,slack,qq")
        self.assertEqual(os.environ["FEISHU_APP_ID"], "app-id")
        self.assertEqual(os.environ["FEISHU_ALLOW_FROM"], "ou_1,ou_2")
        self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "tg-token")
        self.assertEqual(os.environ["TELEGRAM_ALLOW_FROM"], "u1,u2")
        self.assertEqual(os.environ["TELEGRAM_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(os.environ["WHATSAPP_BRIDGE_URL"], "ws://127.0.0.1:3001")
        self.assertEqual(os.environ["WHATSAPP_BRIDGE_TOKEN"], "wa-bridge-token")
        self.assertEqual(os.environ["WHATSAPP_ALLOW_FROM"], "8613800138000,8613900139000")
        self.assertEqual(os.environ["DISCORD_BOT_TOKEN"], "discord-token")
        self.assertEqual(os.environ["DISCORD_ALLOW_FROM"], "du1,du2")
        self.assertEqual(os.environ["DISCORD_POLL_CHANNELS"], "123,456")
        self.assertEqual(os.environ["MOCHAT_BASE_URL"], "https://mochat.io")
        self.assertEqual(os.environ["MOCHAT_CLAW_TOKEN"], "mochat-claw-token")
        self.assertEqual(os.environ["MOCHAT_AGENT_USER_ID"], "agent_u1")
        self.assertEqual(os.environ["MOCHAT_SESSIONS"], "session_1,session_2")
        self.assertEqual(os.environ["MOCHAT_PANELS"], "panel_1,panel_2")
        self.assertEqual(os.environ["MOCHAT_ALLOW_FROM"], "mo_u1,mo_u2")
        self.assertEqual(os.environ["MOCHAT_POLL_INTERVAL_SECONDS"], "9")
        self.assertEqual(os.environ["MOCHAT_WATCH_TIMEOUT_MS"], "12000")
        self.assertEqual(os.environ["MOCHAT_WATCH_LIMIT"], "21")
        self.assertEqual(os.environ["MOCHAT_PANEL_LIMIT"], "88")
        self.assertEqual(os.environ["DINGTALK_CLIENT_ID"], "dt-app-id")
        self.assertEqual(os.environ["DINGTALK_CLIENT_SECRET"], "dt-app-secret")
        self.assertEqual(os.environ["DINGTALK_ALLOW_FROM"], "dt_u1,dt_u2")
        self.assertEqual(os.environ["DINGTALK_STREAM_MODE_ENABLED"], "0")
        self.assertEqual(os.environ["DINGTALK_STREAM_RECONNECT_DELAY_SECONDS"], "13")
        self.assertEqual(os.environ["EMAIL_CONSENT_GRANTED"], "1")
        self.assertEqual(os.environ["EMAIL_SMTP_HOST"], "smtp.example.com")
        self.assertEqual(os.environ["EMAIL_SMTP_USERNAME"], "bot@example.com")
        self.assertEqual(os.environ["EMAIL_SMTP_PASSWORD"], "pw")
        self.assertEqual(os.environ["EMAIL_FROM_ADDRESS"], "bot@example.com")
        self.assertEqual(os.environ["EMAIL_ALLOW_FROM"], "a@example.com,b@example.com")
        self.assertEqual(os.environ["SLACK_BOT_TOKEN"], "xoxb-token")
        self.assertEqual(os.environ["SLACK_APP_TOKEN"], "xapp-token")
        self.assertEqual(os.environ["SLACK_ALLOW_FROM"], "U1,U2")
        self.assertEqual(os.environ["SLACK_POLL_CHANNELS"], "C1,C2")
        self.assertEqual(os.environ["QQ_APP_ID"], "qq-app-id")
        self.assertEqual(os.environ["QQ_SECRET"], "qq-secret")
        self.assertEqual(os.environ["QQ_ALLOW_FROM"], "qq_u1,qq_u2")
        self.assertEqual(os.environ["SENTIENTAGENT_V2_SESSION_DB_URL"], "sqlite+aiosqlite:////tmp/sessions.db")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "google-key")
        self.assertEqual(os.environ["SENTIENTAGENT_V2_WEB_SEARCH_ENABLED"], "0")
        self.assertEqual(os.environ["SENTIENTAGENT_V2_RESTRICT_TO_WORKSPACE"], "1")
        self.assertEqual(os.environ["SENTIENTAGENT_V2_ALLOW_EXEC"], "0")
        self.assertEqual(os.environ["SENTIENTAGENT_V2_ALLOW_NETWORK"], "0")
        self.assertEqual(os.environ["SENTIENTAGENT_V2_EXEC_ALLOWLIST"], "python,ls")
        self.assertNotIn("BRAVE_API_KEY", os.environ)

    def test_bootstrap_env_includes_future_enabled_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["channels"]["local"]["enabled"] = False
            cfg["channels"]["telegram"]["enabled"] = True
            cfg["channels"]["qq"]["enabled"] = True
            save_config(cfg, path)

            os.environ.pop("SENTIENTAGENT_V2_CHANNELS", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["SENTIENTAGENT_V2_CHANNELS"], "telegram,qq")

    def test_bootstrap_env_overwrites_and_clears_managed_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["channels"]["feishu"]["appId"] = ""
            cfg["channels"]["feishu"]["appSecret"] = ""
            cfg["providers"]["google"]["apiKey"] = "from-config"
            save_config(cfg, path)

            os.environ["GOOGLE_API_KEY"] = "from-shell"
            os.environ["FEISHU_APP_ID"] = "stale-feishu-id"
            os.environ["FEISHU_APP_SECRET"] = "stale-feishu-secret"
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["GOOGLE_API_KEY"], "from-config")
        self.assertNotIn("FEISHU_APP_ID", os.environ)
        self.assertNotIn("FEISHU_APP_SECRET", os.environ)

    def test_web_search_api_key_is_loaded_from_web_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["web"]["search"]["apiKey"] = "brave-key"
            save_config(cfg, path)

            os.environ.pop("BRAVE_API_KEY", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["BRAVE_API_KEY"], "brave-key")

    def test_provider_selection_uses_enabled_flags_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["providers"]["google"]["enabled"] = False
            cfg["providers"]["google"]["apiKey"] = "google-key-ignored"
            cfg["providers"]["openai"]["enabled"] = True
            cfg["providers"]["openai"]["apiKey"] = "openai-key-selected"
            cfg["providers"]["openai"]["model"] = "gpt-4.1-mini"
            save_config(cfg, path)

            os.environ.pop("SENTIENTAGENT_V2_PROVIDER", None)
            os.environ.pop("SENTIENTAGENT_V2_PROVIDER_ENABLED", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("SENTIENTAGENT_V2_MODEL", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["SENTIENTAGENT_V2_PROVIDER"], "openai")
        self.assertEqual(os.environ["SENTIENTAGENT_V2_PROVIDER_ENABLED"], "1")
        self.assertEqual(os.environ["OPENAI_API_KEY"], "openai-key-selected")
        self.assertNotIn("GOOGLE_API_KEY", os.environ)
        self.assertEqual(os.environ["SENTIENTAGENT_V2_MODEL"], "openai/gpt-4.1-mini")

    def test_provider_api_base_and_extra_headers_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["providers"]["google"]["enabled"] = False
            cfg["providers"]["openrouter"]["enabled"] = True
            cfg["providers"]["openrouter"]["apiKey"] = "openrouter-key"
            cfg["providers"]["openrouter"]["model"] = "openai/gpt-4.1-mini"
            cfg["providers"]["openrouter"]["apiBase"] = "https://example.gateway/v1"
            cfg["providers"]["openrouter"]["extraHeaders"] = {"X-Trace-Id": "trace-001"}
            save_config(cfg, path)

            os.environ.pop("SENTIENTAGENT_V2_PROVIDER", None)
            os.environ.pop("SENTIENTAGENT_V2_PROVIDER_API_BASE", None)
            os.environ.pop("SENTIENTAGENT_V2_PROVIDER_EXTRA_HEADERS_JSON", None)
            os.environ.pop("OPENROUTER_API_KEY", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["SENTIENTAGENT_V2_PROVIDER"], "openrouter")
        self.assertEqual(os.environ["OPENROUTER_API_KEY"], "openrouter-key")
        self.assertEqual(os.environ["SENTIENTAGENT_V2_PROVIDER_API_BASE"], "https://example.gateway/v1")
        self.assertEqual(
            os.environ["SENTIENTAGENT_V2_PROVIDER_EXTRA_HEADERS_JSON"],
            '{"X-Trace-Id":"trace-001"}',
        )

    def test_provider_active_key_is_ignored_when_enabled_points_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["providers"]["google"]["enabled"] = True
            cfg["providers"]["google"]["apiKey"] = "google-key-selected"
            cfg["providers"]["openai"]["enabled"] = False
            cfg["providers"]["openai"]["apiKey"] = "openai-key-ignored"
            cfg["providers"]["active"] = "openai"
            save_config(cfg, path)

            os.environ.pop("SENTIENTAGENT_V2_PROVIDER", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["SENTIENTAGENT_V2_PROVIDER"], "google")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "google-key-selected")
        self.assertNotIn("OPENAI_API_KEY", os.environ)

    def test_legacy_keys_are_not_used_anymore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["providers"]["google"]["apiKey"] = ""
            cfg["web"]["search"]["apiKey"] = ""
            cfg["keys"] = {"googleApiKey": "legacy-google", "braveApiKey": "legacy-brave"}
            save_config(cfg, path)
            loaded_cfg = load_config(path)

            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("BRAVE_API_KEY", None)
            bootstrap_env_from_config(path)

        self.assertNotIn("keys", loaded_cfg)
        self.assertNotIn("GOOGLE_API_KEY", os.environ)
        self.assertNotIn("OPENAI_API_KEY", os.environ)
        self.assertNotIn("BRAVE_API_KEY", os.environ)

    def test_mcp_servers_are_exported_to_env_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["tools"]["mcpServers"] = {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                }
            }
            save_config(cfg, path)

            os.environ.pop("SENTIENTAGENT_V2_MCP_SERVERS_JSON", None)
            bootstrap_env_from_config(path)

        raw = os.environ.get("SENTIENTAGENT_V2_MCP_SERVERS_JSON")
        self.assertIsNotNone(raw)
        parsed = json.loads(raw or "{}")
        self.assertIn("filesystem", parsed)
        self.assertEqual(parsed["filesystem"]["command"], "npx")


if __name__ == "__main__":
    unittest.main()
