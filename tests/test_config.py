"""Tests for persistent config helpers."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from openpipixia.core.config import (
    apply_agent_privilege_level_defaults,
    apply_config_to_env,
    bootstrap_env_from_config,
    config_to_env,
    default_config,
    get_config_path,
    get_data_dir,
    get_runtime_config_path,
    load_runtime_config,
    load_config,
    save_config,
    save_runtime_config,
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
        self.assertIn("dingtalk", cfg["channels"])
        self.assertIn("email", cfg["channels"])
        self.assertIn("slack", cfg["channels"])
        self.assertIn("qq", cfg["channels"])
        self.assertIn("weixin", cfg["channels"])
        self.assertIn("wecom", cfg["channels"])
        self.assertTrue(cfg["providers"]["google"]["enabled"])
        self.assertEqual(cfg["providers"]["openai"]["model"], "openai/gpt-5.4")
        self.assertEqual(cfg["providers"]["openrouter"]["model"], "openai/gpt-5.4")
        self.assertEqual(cfg["providers"]["aihubmix"]["model"], "openai/gpt-5.4")
        self.assertEqual(cfg["providers"]["custom"]["model"], "openai/gpt-5.4")
        self.assertIn("openai_mm", cfg["multimodalProviders"])
        self.assertIn("google_mm", cfg["multimodalProviders"])
        self.assertIn("qwen_mm", cfg["multimodalProviders"])
        self.assertFalse(cfg["multimodalProviders"]["openai_mm"]["enabled"])
        self.assertEqual(cfg["multimodalProviders"]["openai_mm"]["provider"], "openai")
        self.assertEqual(cfg["multimodalProviders"]["openai_mm"]["model"], "gpt-5.4")
        self.assertEqual(cfg["multimodalProviders"]["google_mm"]["provider"], "google")
        self.assertEqual(cfg["multimodalProviders"]["qwen_mm"]["provider"], "qwen")
        self.assertEqual(cfg["gui"]["groundingProvider"], "openai_mm")
        self.assertEqual(cfg["gui"]["plannerProvider"], "openai_mm")
        self.assertTrue(cfg["gui"]["builtinGUIToolsEnabled"])
        self.assertTrue(cfg["web"]["search"]["enabled"])
        self.assertEqual(cfg["session"]["dbUrl"], "")
        self.assertEqual(cfg["agent"]["heartbeat"]["every"], "30m")
        self.assertEqual(cfg["agent"]["heartbeat"]["ackMaxChars"], 300)
        self.assertFalse(cfg["agent"]["heartbeat"]["showOk"])
        self.assertTrue(cfg["agent"]["heartbeat"]["showAlerts"])
        self.assertEqual(cfg["agent"]["heartbeat"]["target"], "last")
        self.assertEqual(cfg["agent"]["heartbeat"]["targetChannel"], "")
        self.assertEqual(cfg["agent"]["heartbeat"]["targetChatId"], "")
        self.assertEqual(cfg["agent"]["heartbeat"]["activeHours"]["start"], "")
        self.assertEqual(cfg["agent"]["heartbeat"]["activeHours"]["end"], "")
        self.assertEqual(cfg["agent"]["heartbeat"]["activeHours"]["timezone"], "user")
        self.assertEqual(cfg["agent"]["name"], "")
        self.assertEqual(cfg["agent"]["privilegeLevel"], "")
        self.assertEqual(cfg["agent"]["permissions"], {})
        self.assertFalse(cfg["security"]["restrictToWorkspace"])
        self.assertTrue(cfg["security"]["allowExec"])
        self.assertTrue(cfg["security"]["allowNetwork"])
        self.assertEqual(cfg["security"]["execAllowlist"], [])
        self.assertEqual(cfg["tools"]["mcpServers"], {})
        self.assertNotIn("env", cfg)

    def test_path_helpers_follow_explicit_config_env_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "agent_1" / "config.json"
            runtime_path = config_path.with_name("runtime.json")
            os.environ["OPENPPX_CONFIG_FILE"] = str(config_path)
            os.environ.pop("OPENPPX_RUNTIME_CONFIG_FILE", None)
            os.environ.pop("OPENPPX_DATA_DIR", None)

            self.assertEqual(get_config_path(), config_path)
            self.assertEqual(get_runtime_config_path(), runtime_path)
            self.assertEqual(get_data_dir(), Path.home() / ".openppx")

    def test_bootstrap_sets_process_config_runtime_data_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "agent_a" / "config.json"
            cfg = default_config()
            save_config(cfg, config_path)
            os.environ.pop("OPENPPX_CONFIG_FILE", None)
            os.environ.pop("OPENPPX_RUNTIME_CONFIG_FILE", None)
            os.environ.pop("OPENPPX_DATA_DIR", None)

            loaded = bootstrap_env_from_config(config_path)

        self.assertIsNotNone(loaded)
        self.assertEqual(
            Path(os.environ["OPENPPX_CONFIG_FILE"]).resolve(),
            config_path.resolve(),
        )
        self.assertEqual(
            Path(os.environ["OPENPPX_RUNTIME_CONFIG_FILE"]).resolve(),
            config_path.with_name("runtime.json").resolve(),
        )
        self.assertEqual(
            Path(os.environ["OPENPPX_DATA_DIR"]).resolve(),
            config_path.parent.resolve(),
        )

    def test_load_runtime_missing_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_cfg = load_runtime_config(Path(tmp) / "runtime.json")
        self.assertTrue(runtime_cfg["env"]["OPENPPX_MEMORY_ENABLED"])
        self.assertEqual(runtime_cfg["env"]["OPENPPX_MEMORY_BACKEND"], "markdown")
        self.assertEqual(runtime_cfg["env"]["OPENPPX_COMPACTION_INTERVAL"], 8)
        self.assertEqual(runtime_cfg["env"]["OPENPPX_MCP_DOCTOR_TIMEOUT_SECONDS"], 5)
        self.assertEqual(runtime_cfg["env"]["OPENPPX_DEBUG_MAX_CHARS"], 0)

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
        os.environ["OPENPPX_MODEL"] = "from-shell"
        os.environ["GOOGLE_API_KEY"] = "key-from-shell"
        cfg = default_config()
        cfg["providers"]["google"]["model"] = "from-config"
        cfg["providers"]["google"]["apiKey"] = "key-from-config"
        apply_config_to_env(cfg, overwrite=False)
        self.assertEqual(os.environ["OPENPPX_MODEL"], "from-shell")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "key-from-shell")

        apply_config_to_env(cfg, overwrite=True)
        self.assertEqual(os.environ["OPENPPX_MODEL"], "from-config")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "key-from-config")

    def test_apply_config_to_env_preserves_explicit_shell_debug_flags(self) -> None:
        cfg = default_config()
        cfg["debug"] = False

        os.environ["OPENPPX_DEBUG"] = "1"
        os.environ["OPENPPX_DEBUG_LOG_PATH"] = "/tmp/openppx-debug.log"

        apply_config_to_env(cfg, overwrite=True)

        self.assertEqual(os.environ["OPENPPX_DEBUG"], "1")
        self.assertEqual(os.environ["OPENPPX_DEBUG_LOG_PATH"], "/tmp/openppx-debug.log")

    def test_apply_agent_privilege_level_defaults_syncs_low_permissions(self) -> None:
        cfg = default_config()
        apply_agent_privilege_level_defaults(cfg, privilege_level="low")

        self.assertEqual(cfg["agent"]["privilegeLevel"], "low")
        self.assertEqual(cfg["agent"]["permissions"]["filesystemAccess"], "read_only")
        self.assertTrue(cfg["security"]["restrictToWorkspace"])
        self.assertFalse(cfg["security"]["allowExec"])
        self.assertFalse(cfg["security"]["allowNetwork"])
        self.assertEqual(cfg["security"]["execAllowlist"], [])

    def test_apply_agent_privilege_level_defaults_syncs_medium_permissions(self) -> None:
        cfg = default_config()
        apply_agent_privilege_level_defaults(cfg, privilege_level="medium")
        env = config_to_env(cfg)

        self.assertEqual(cfg["agent"]["privilegeLevel"], "medium")
        self.assertEqual(cfg["agent"]["permissions"]["filesystemAccess"], "read_write")
        self.assertTrue(cfg["security"]["restrictToWorkspace"])
        self.assertTrue(cfg["security"]["allowExec"])
        self.assertTrue(cfg["security"]["allowNetwork"])
        self.assertEqual(env["OPENPPX_AGENT_PRIVILEGE_LEVEL"], "medium")
        self.assertEqual(env["OPENPPX_FILESYSTEM_ACCESS"], "read_write")

    def test_apply_agent_privilege_level_defaults_syncs_high_permissions(self) -> None:
        cfg = default_config()
        apply_agent_privilege_level_defaults(cfg, privilege_level="high")

        self.assertEqual(cfg["agent"]["privilegeLevel"], "high")
        self.assertEqual(cfg["agent"]["permissions"]["filesystemAccess"], "read_write")
        self.assertFalse(cfg["security"]["restrictToWorkspace"])
        self.assertTrue(cfg["security"]["allowExec"])
        self.assertTrue(cfg["security"]["allowNetwork"])
        self.assertEqual(cfg["security"]["execAllowlist"], [])

    def test_load_config_rejects_legacy_agent_role_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"agent":{"role":"assistant"}}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "agent.role"):
                load_config(path)

    def test_load_config_rejects_unknown_privilege_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"agent":{"privilegeLevel":"assistant"}}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unsupported agent privilege level"):
                load_config(path)

    def test_default_channel_streaming_flags(self) -> None:
        cfg = default_config()

        self.assertTrue(cfg["channels"]["local"]["streamingEnabled"])
        self.assertFalse(cfg["channels"]["feishu"]["streamingEnabled"])

    def test_config_to_env_maps_channel_streaming_flags(self) -> None:
        cfg = default_config()
        cfg["channels"]["local"]["streamingEnabled"] = False
        cfg["channels"]["feishu"]["streamingEnabled"] = True

        env = config_to_env(cfg)

        self.assertEqual(env["LOCAL_STREAMING_ENABLED"], "0")
        self.assertEqual(env["FEISHU_STREAMING_ENABLED"], "1")

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
            cfg["channels"]["weixin"]["enabled"] = True
            cfg["channels"]["weixin"]["baseUrl"] = "https://wx.example.com"
            cfg["channels"]["weixin"]["token"] = "wx-token"
            cfg["channels"]["weixin"]["stateDir"] = "runtime/weixin"
            cfg["channels"]["weixin"]["pollTimeoutSeconds"] = 41
            cfg["channels"]["weixin"]["allowFrom"] = ["wx_u1", "wx_u2"]
            cfg["channels"]["wecom"]["enabled"] = True
            cfg["channels"]["wecom"]["botId"] = "wecom-bot-id"
            cfg["channels"]["wecom"]["secret"] = "wecom-secret"
            cfg["channels"]["wecom"]["allowFrom"] = ["wc_u1", "wc_u2"]
            cfg["channels"]["wecom"]["welcomeMessage"] = "hello"
            cfg["session"]["dbUrl"] = "sqlite+aiosqlite:////tmp/sessions.db"
            cfg["agent"]["heartbeat"]["every"] = "15m"
            cfg["agent"]["heartbeat"]["prompt"] = "ops check"
            cfg["agent"]["heartbeat"]["ackMaxChars"] = 120
            cfg["agent"]["heartbeat"]["showOk"] = True
            cfg["agent"]["heartbeat"]["showAlerts"] = False
            cfg["agent"]["heartbeat"]["target"] = "channel"
            cfg["agent"]["heartbeat"]["targetChannel"] = "feishu"
            cfg["agent"]["heartbeat"]["targetChatId"] = "ops-room"
            cfg["agent"]["heartbeat"]["activeHours"]["start"] = "09:00"
            cfg["agent"]["heartbeat"]["activeHours"]["end"] = "18:00"
            cfg["agent"]["heartbeat"]["activeHours"]["timezone"] = "UTC"
            cfg["providers"]["google"]["apiKey"] = "google-key"
            cfg["web"]["search"]["enabled"] = False
            cfg["security"]["restrictToWorkspace"] = True
            cfg["security"]["allowExec"] = False
            cfg["security"]["allowNetwork"] = False
            cfg["security"]["execAllowlist"] = ["python", "ls", "python"]
            save_config(cfg, path)

            os.environ.pop("OPENPPX_CHANNELS", None)
            os.environ.pop("OPENPPX_HEARTBEAT_EVERY", None)
            os.environ.pop("OPENPPX_HEARTBEAT_PROMPT", None)
            os.environ.pop("OPENPPX_HEARTBEAT_ACK_MAX_CHARS", None)
            os.environ.pop("OPENPPX_HEARTBEAT_SHOW_OK", None)
            os.environ.pop("OPENPPX_HEARTBEAT_SHOW_ALERTS", None)
            os.environ.pop("OPENPPX_HEARTBEAT_TARGET", None)
            os.environ.pop("OPENPPX_HEARTBEAT_TARGET_CHANNEL", None)
            os.environ.pop("OPENPPX_HEARTBEAT_TARGET_CHAT_ID", None)
            os.environ.pop("OPENPPX_HEARTBEAT_ACTIVE_HOURS_START", None)
            os.environ.pop("OPENPPX_HEARTBEAT_ACTIVE_HOURS_END", None)
            os.environ.pop("OPENPPX_HEARTBEAT_ACTIVE_HOURS_TIMEZONE", None)
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
            os.environ.pop("WEIXIN_BASE_URL", None)
            os.environ.pop("WEIXIN_TOKEN", None)
            os.environ.pop("WEIXIN_STATE_DIR", None)
            os.environ.pop("WEIXIN_POLL_TIMEOUT_SECONDS", None)
            os.environ.pop("WEIXIN_ALLOW_FROM", None)
            os.environ.pop("WECOM_BOT_ID", None)
            os.environ.pop("WECOM_SECRET", None)
            os.environ.pop("WECOM_ALLOW_FROM", None)
            os.environ.pop("WECOM_WELCOME_MESSAGE", None)
            os.environ.pop("OPENPPX_SESSION_DB_URL", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("BRAVE_API_KEY", None)
            os.environ.pop("OPENPPX_WEB_SEARCH_ENABLED", None)
            os.environ.pop("OPENPPX_RESTRICT_TO_WORKSPACE", None)
            os.environ.pop("OPENPPX_ALLOW_EXEC", None)
            os.environ.pop("OPENPPX_ALLOW_NETWORK", None)
            os.environ.pop("OPENPPX_EXEC_ALLOWLIST", None)
            loaded = bootstrap_env_from_config(path)

        self.assertIsNotNone(loaded)
        self.assertEqual(os.environ["OPENPPX_CHANNELS"], "feishu,telegram,whatsapp,discord,dingtalk,email,slack,qq,weixin,wecom")
        self.assertEqual(os.environ["OPENPPX_HEARTBEAT_EVERY"], "15m")
        self.assertEqual(os.environ["OPENPPX_HEARTBEAT_PROMPT"], "ops check")
        self.assertEqual(os.environ["OPENPPX_HEARTBEAT_ACK_MAX_CHARS"], "120")
        self.assertEqual(os.environ["OPENPPX_HEARTBEAT_SHOW_OK"], "1")
        self.assertEqual(os.environ["OPENPPX_HEARTBEAT_SHOW_ALERTS"], "0")
        self.assertEqual(os.environ["OPENPPX_HEARTBEAT_TARGET"], "channel")
        self.assertEqual(os.environ["OPENPPX_HEARTBEAT_TARGET_CHANNEL"], "feishu")
        self.assertEqual(os.environ["OPENPPX_HEARTBEAT_TARGET_CHAT_ID"], "ops-room")
        self.assertEqual(os.environ["OPENPPX_HEARTBEAT_ACTIVE_HOURS_START"], "09:00")
        self.assertEqual(os.environ["OPENPPX_HEARTBEAT_ACTIVE_HOURS_END"], "18:00")
        self.assertEqual(os.environ["OPENPPX_HEARTBEAT_ACTIVE_HOURS_TIMEZONE"], "UTC")
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
        self.assertEqual(os.environ["WEIXIN_BASE_URL"], "https://wx.example.com")
        self.assertEqual(os.environ["WEIXIN_TOKEN"], "wx-token")
        self.assertEqual(os.environ["WEIXIN_STATE_DIR"], "runtime/weixin")
        self.assertEqual(os.environ["WEIXIN_POLL_TIMEOUT_SECONDS"], "41")
        self.assertEqual(os.environ["WEIXIN_ALLOW_FROM"], "wx_u1,wx_u2")
        self.assertEqual(os.environ["WECOM_BOT_ID"], "wecom-bot-id")
        self.assertEqual(os.environ["WECOM_SECRET"], "wecom-secret")
        self.assertEqual(os.environ["WECOM_ALLOW_FROM"], "wc_u1,wc_u2")
        self.assertEqual(os.environ["WECOM_WELCOME_MESSAGE"], "hello")
        self.assertEqual(os.environ["OPENPPX_SESSION_DB_URL"], "sqlite+aiosqlite:////tmp/sessions.db")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "google-key")
        self.assertEqual(os.environ["OPENPPX_WEB_SEARCH_ENABLED"], "0")
        self.assertEqual(os.environ["OPENPPX_RESTRICT_TO_WORKSPACE"], "1")
        self.assertEqual(os.environ["OPENPPX_ALLOW_EXEC"], "0")
        self.assertEqual(os.environ["OPENPPX_ALLOW_NETWORK"], "0")
        self.assertEqual(os.environ["OPENPPX_EXEC_ALLOWLIST"], "python,ls")
        self.assertNotIn("BRAVE_API_KEY", os.environ)

    def test_bootstrap_env_from_empty_object_falls_back_to_shell_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{}\n", encoding="utf-8")

            os.environ["OPENPPX_PROVIDER"] = "openai"
            os.environ["OPENPPX_MEMORY_BACKEND"] = "in_memory"
            loaded = bootstrap_env_from_config(path)

        self.assertIsNone(loaded)
        self.assertEqual(os.environ["OPENPPX_PROVIDER"], "openai")
        self.assertEqual(os.environ["OPENPPX_MEMORY_BACKEND"], "in_memory")

    def test_apply_config_to_env_clamps_invalid_heartbeat_ack_max_chars(self) -> None:
        cfg = default_config()
        cfg["agent"]["heartbeat"]["ackMaxChars"] = -99
        apply_config_to_env(cfg, overwrite=True)
        self.assertEqual(os.environ["OPENPPX_HEARTBEAT_ACK_MAX_CHARS"], "0")

    def test_bootstrap_env_supports_env_override_map_for_runtime_knobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["env"] = {
                "OPENPPX_MEMORY_ENABLED": "0",
                "OPENPPX_MEMORY_BACKEND": "in_memory",
                "OPENPPX_MEMORY_MARKDOWN_DIR": "/tmp/custom-memory",
                "OPENPPX_COMPACTION_INTERVAL": 12,
                "OPENPPX_MCP_REQUIRED_SERVERS": "filesystem,notion",
                "OPENPPX_SUBAGENT_MAX_CONCURRENCY": 4,
                "OPENPPX_BOOTSTRAP_MAX_TOTAL_CHARS": 48000,
                "OPENPPX_DEBUG_MAX_CHARS": 4000,
                "OPENPPX_WHATSAPP_BRIDGE_PRECHECK": False,
            }
            save_config(cfg, path)

            os.environ.pop("OPENPPX_MEMORY_ENABLED", None)
            os.environ.pop("OPENPPX_MEMORY_BACKEND", None)
            os.environ.pop("OPENPPX_MEMORY_MARKDOWN_DIR", None)
            os.environ.pop("OPENPPX_COMPACTION_INTERVAL", None)
            os.environ.pop("OPENPPX_MCP_REQUIRED_SERVERS", None)
            os.environ.pop("OPENPPX_SUBAGENT_MAX_CONCURRENCY", None)
            os.environ.pop("OPENPPX_BOOTSTRAP_MAX_TOTAL_CHARS", None)
            os.environ.pop("OPENPPX_DEBUG_MAX_CHARS", None)
            os.environ.pop("OPENPPX_WHATSAPP_BRIDGE_PRECHECK", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["OPENPPX_MEMORY_ENABLED"], "0")
        self.assertEqual(os.environ["OPENPPX_MEMORY_BACKEND"], "in_memory")
        self.assertEqual(os.environ["OPENPPX_MEMORY_MARKDOWN_DIR"], "/tmp/custom-memory")
        self.assertEqual(os.environ["OPENPPX_COMPACTION_INTERVAL"], "12")
        self.assertEqual(os.environ["OPENPPX_MCP_REQUIRED_SERVERS"], "filesystem,notion")
        self.assertEqual(os.environ["OPENPPX_SUBAGENT_MAX_CONCURRENCY"], "4")
        self.assertEqual(os.environ["OPENPPX_BOOTSTRAP_MAX_TOTAL_CHARS"], "48000")
        self.assertEqual(os.environ["OPENPPX_DEBUG_MAX_CHARS"], "4000")
        self.assertEqual(os.environ["OPENPPX_WHATSAPP_BRIDGE_PRECHECK"], "0")

    def test_save_config_migrates_legacy_env_to_runtime_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            runtime_path = Path(tmp) / "runtime.json"
            cfg = default_config()
            cfg["env"] = {
                "OPENPPX_MEMORY_BACKEND": "in_memory",
                "OPENPPX_DEBUG_MAX_CHARS": 4096,
            }

            save_config(cfg, config_path)

            saved_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            saved_runtime = load_runtime_config(runtime_path)

        self.assertNotIn("env", saved_cfg)
        self.assertEqual(saved_runtime["env"]["OPENPPX_MEMORY_BACKEND"], "in_memory")
        self.assertEqual(saved_runtime["env"]["OPENPPX_DEBUG_MAX_CHARS"], 4096)

    def test_bootstrap_env_prefers_runtime_file_over_legacy_config_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            runtime_path = Path(tmp) / "runtime.json"

            config_path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "google": {
                                "enabled": True,
                                "apiKey": "",
                                "model": "gemini-2.5-flash",
                                "apiBase": "",
                                "extraHeaders": {},
                            }
                        },
                        "env": {"OPENPPX_MEMORY_BACKEND": "in_memory"},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            save_runtime_config({"env": {"OPENPPX_MEMORY_BACKEND": "markdown"}}, runtime_path)
            os.environ.pop("OPENPPX_MEMORY_BACKEND", None)
            bootstrap_env_from_config(config_path)

        self.assertEqual(os.environ["OPENPPX_MEMORY_BACKEND"], "markdown")

    def test_bootstrap_default_runtime_env_values_are_visible_and_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(default_config(), path)

            os.environ.pop("OPENPPX_MEMORY_ENABLED", None)
            os.environ.pop("OPENPPX_MEMORY_BACKEND", None)
            os.environ.pop("OPENPPX_COMPACTION_INTERVAL", None)
            os.environ.pop("OPENPPX_MCP_DOCTOR_TIMEOUT_SECONDS", None)
            os.environ.pop("OPENPPX_DEBUG_MAX_CHARS", None)
            os.environ.pop("OPENPPX_COMPACTION_TOKEN_THRESHOLD", None)
            os.environ.pop("OPENPPX_MCP_REQUIRED_SERVERS", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["OPENPPX_MEMORY_ENABLED"], "1")
        self.assertEqual(os.environ["OPENPPX_MEMORY_BACKEND"], "markdown")
        self.assertEqual(os.environ["OPENPPX_COMPACTION_INTERVAL"], "8")
        self.assertEqual(os.environ["OPENPPX_MCP_DOCTOR_TIMEOUT_SECONDS"], "5")
        self.assertEqual(os.environ["OPENPPX_DEBUG_MAX_CHARS"], "0")
        self.assertNotIn("OPENPPX_MEMORY_MARKDOWN_DIR", os.environ)
        self.assertNotIn("OPENPPX_COMPACTION_TOKEN_THRESHOLD", os.environ)
        self.assertNotIn("OPENPPX_MCP_REQUIRED_SERVERS", os.environ)

    def test_bootstrap_remaps_legacy_global_memory_markdown_dir_to_agent_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / "agent_1"
            config_path = agent_dir / "config.json"
            runtime_path = agent_dir / "runtime.json"
            save_config(default_config(), config_path)
            save_runtime_config(
                {
                    "env": {
                        "OPENPPX_MEMORY_MARKDOWN_DIR": str(Path.home() / ".openpipixia" / "workspace" / "memory"),
                    }
                },
                runtime_path,
            )
            os.environ.pop("OPENPPX_MEMORY_MARKDOWN_DIR", None)

            bootstrap_env_from_config(config_path)

        self.assertEqual(
            os.environ["OPENPPX_MEMORY_MARKDOWN_DIR"],
            str((agent_dir / "memory").resolve(strict=False)),
        )

    def test_env_override_map_is_final_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["providers"]["google"]["apiKey"] = "google-key-from-provider"
            cfg["env"] = {
                "GOOGLE_API_KEY": "google-key-from-env-map",
            }
            save_config(cfg, path)

            os.environ.pop("GOOGLE_API_KEY", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["GOOGLE_API_KEY"], "google-key-from-env-map")

    def test_bootstrap_env_includes_future_enabled_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["channels"]["local"]["enabled"] = False
            cfg["channels"]["telegram"]["enabled"] = True
            cfg["channels"]["qq"]["enabled"] = True
            save_config(cfg, path)

            os.environ.pop("OPENPPX_CHANNELS", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["OPENPPX_CHANNELS"], "telegram,qq")

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

    def test_bootstrap_env_keeps_shell_api_key_when_active_provider_key_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["providers"]["google"]["apiKey"] = ""
            save_config(cfg, path)

            os.environ["GOOGLE_API_KEY"] = "google-key-from-shell"
            os.environ["OPENAI_API_KEY"] = "stale-openai-key"
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["GOOGLE_API_KEY"], "google-key-from-shell")
        self.assertNotIn("OPENAI_API_KEY", os.environ)

    def test_bootstrap_env_keeps_shell_api_key_for_non_default_active_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["providers"]["google"]["enabled"] = False
            cfg["providers"]["google"]["apiKey"] = ""
            cfg["providers"]["openai"]["enabled"] = True
            cfg["providers"]["openai"]["apiKey"] = ""
            save_config(cfg, path)

            os.environ["OPENAI_API_KEY"] = "openai-key-from-shell"
            os.environ["GOOGLE_API_KEY"] = "stale-google-key"
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["OPENPPX_PROVIDER"], "openai")
        self.assertEqual(os.environ["OPENAI_API_KEY"], "openai-key-from-shell")
        self.assertNotIn("GOOGLE_API_KEY", os.environ)

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

            os.environ.pop("OPENPPX_PROVIDER", None)
            os.environ.pop("OPENPPX_PROVIDER_ENABLED", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("OPENPPX_MODEL", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["OPENPPX_PROVIDER"], "openai")
        self.assertEqual(os.environ["OPENPPX_PROVIDER_ENABLED"], "1")
        self.assertEqual(os.environ["OPENAI_API_KEY"], "openai-key-selected")
        self.assertNotIn("GOOGLE_API_KEY", os.environ)
        self.assertEqual(os.environ["OPENPPX_MODEL"], "openai/gpt-4.1-mini")

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

            os.environ.pop("OPENPPX_PROVIDER", None)
            os.environ.pop("OPENPPX_PROVIDER_API_BASE", None)
            os.environ.pop("OPENPPX_PROVIDER_EXTRA_HEADERS_JSON", None)
            os.environ.pop("OPENROUTER_API_KEY", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["OPENPPX_PROVIDER"], "openrouter")
        self.assertEqual(os.environ["OPENROUTER_API_KEY"], "openrouter-key")
        self.assertEqual(os.environ["OPENPPX_PROVIDER_API_BASE"], "https://example.gateway/v1")
        self.assertEqual(
            os.environ["OPENPPX_PROVIDER_EXTRA_HEADERS_JSON"],
            '{"X-Trace-Id":"trace-001"}',
        )

    def test_gui_multimodal_providers_are_mapped_to_gui_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["multimodalProviders"] = {
                "google": {
                    "enabled": True,
                    "apiKey": "grounding-key",
                    "model": "gemini-3-flash-preview",
                    "apiBase": "https://grounding.example/v1",
                    "extraHeaders": {},
                },
                "openai": {
                    "enabled": True,
                    "apiKey": "planner-key",
                    "model": "openai/gpt-4.1-mini",
                    "apiBase": "https://planner.example/v1",
                    "extraHeaders": {},
                },
            }
            cfg["gui"]["groundingProvider"] = "google"
            cfg["gui"]["plannerProvider"] = "openai"
            save_config(cfg, path)

            os.environ.pop("OPENPPX_GUI_MODEL", None)
            os.environ.pop("OPENPPX_GUI_BASE_URL", None)
            os.environ.pop("OPENPPX_GUI_PLANNER_MODEL", None)
            os.environ.pop("OPENPPX_GUI_PLANNER_BASE_URL", None)
            os.environ.pop("OPENPPX_GUI_GROUNDING_PROVIDER", None)
            os.environ.pop("OPENPPX_GUI_PLANNER_PROVIDER", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["OPENPPX_GUI_MODEL"], "gemini-3-flash-preview")
        self.assertEqual(os.environ["OPENPPX_GUI_BASE_URL"], "https://grounding.example/v1")
        self.assertEqual(os.environ["OPENPPX_GUI_GROUNDING_PROVIDER"], "google")
        self.assertEqual(os.environ["OPENPPX_GUI_PLANNER_MODEL"], "openai/gpt-4.1-mini")
        self.assertEqual(os.environ["OPENPPX_GUI_PLANNER_BASE_URL"], "https://planner.example/v1")
        self.assertEqual(os.environ["OPENPPX_GUI_PLANNER_PROVIDER"], "openai")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "grounding-key")
        self.assertEqual(os.environ["OPENAI_API_KEY"], "planner-key")
        self.assertEqual(os.environ["OPENPPX_GUI_BUILTIN_TOOLS_ENABLED"], "1")

    def test_gui_builtin_tools_flag_is_exported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["gui"]["builtinGUIToolsEnabled"] = False
            save_config(cfg, path)

            os.environ.pop("OPENPPX_GUI_BUILTIN_TOOLS_ENABLED", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["OPENPPX_GUI_BUILTIN_TOOLS_ENABLED"], "0")

    def test_gui_multimodal_provider_disabled_does_not_export_gui_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["multimodalProviders"] = {
                "google": {
                    "enabled": False,
                    "apiKey": "grounding-key",
                    "model": "gemini-3-flash-preview",
                    "apiBase": "https://grounding.example/v1",
                    "extraHeaders": {},
                }
            }
            cfg["gui"]["groundingProvider"] = "google"
            save_config(cfg, path)

            os.environ["OPENPPX_GUI_MODEL"] = "stale-model"
            os.environ["OPENPPX_GUI_BASE_URL"] = "https://stale.example/v1"
            os.environ["OPENPPX_GUI_GROUNDING_PROVIDER"] = "openai"
            bootstrap_env_from_config(path)

        self.assertNotIn("OPENPPX_GUI_MODEL", os.environ)
        self.assertNotIn("OPENPPX_GUI_BASE_URL", os.environ)
        self.assertEqual(os.environ.get("OPENPPX_GUI_GROUNDING_PROVIDER", ""), "google")

    def test_gui_multimodal_google_provider_api_key_falls_back_to_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["multimodalProviders"] = {
                "google": {
                    "enabled": True,
                    "apiKey": "",
                    "model": "gemini-3-flash-preview",
                    "apiBase": "",
                    "extraHeaders": {},
                }
            }
            cfg["gui"]["groundingProvider"] = "google"
            save_config(cfg, path)

            os.environ["GOOGLE_API_KEY"] = "key-from-shell"
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["GOOGLE_API_KEY"], "key-from-shell")
        self.assertEqual(os.environ["OPENPPX_GUI_GROUNDING_PROVIDER"], "google")

    def test_gui_multimodal_alias_with_provider_field_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["multimodalProviders"] = {
                "openai_grounding_fast": {
                    "enabled": True,
                    "provider": "openai",
                    "apiKey": "shared-openai-key",
                    "model": "openai/gpt-4.1-mini",
                    "apiBase": "https://openai-grounding.example/v1",
                    "extraHeaders": {},
                },
                "openai_planner_strong": {
                    "enabled": True,
                    "provider": "openai",
                    "apiKey": "shared-openai-key",
                    "model": "openai/gpt-5.2",
                    "apiBase": "https://openai-planner.example/v1",
                    "extraHeaders": {},
                },
            }
            cfg["gui"]["groundingProvider"] = "openai_grounding_fast"
            cfg["gui"]["plannerProvider"] = "openai_planner_strong"
            save_config(cfg, path)

            os.environ.pop("OPENPPX_GUI_MODEL", None)
            os.environ.pop("OPENPPX_GUI_BASE_URL", None)
            os.environ.pop("OPENPPX_GUI_PLANNER_MODEL", None)
            os.environ.pop("OPENPPX_GUI_PLANNER_BASE_URL", None)
            os.environ.pop("OPENPPX_GUI_GROUNDING_PROVIDER", None)
            os.environ.pop("OPENPPX_GUI_PLANNER_PROVIDER", None)
            os.environ.pop("OPENAI_API_KEY", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["OPENPPX_GUI_MODEL"], "openai/gpt-4.1-mini")
        self.assertEqual(os.environ["OPENPPX_GUI_BASE_URL"], "https://openai-grounding.example/v1")
        self.assertEqual(os.environ["OPENPPX_GUI_GROUNDING_PROVIDER"], "openai")
        self.assertEqual(os.environ["OPENPPX_GUI_PLANNER_MODEL"], "openai/gpt-5.2")
        self.assertEqual(os.environ["OPENPPX_GUI_PLANNER_BASE_URL"], "https://openai-planner.example/v1")
        self.assertEqual(os.environ["OPENPPX_GUI_PLANNER_PROVIDER"], "openai")
        self.assertEqual(os.environ["OPENAI_API_KEY"], "shared-openai-key")

    def test_gui_multimodal_alias_without_provider_keeps_backward_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = default_config()
            cfg["multimodalProviders"] = {
                "google": {
                    "enabled": True,
                    "apiKey": "",
                    "model": "gemini-3-flash-preview",
                    "apiBase": "",
                    "extraHeaders": {},
                }
            }
            cfg["gui"]["groundingProvider"] = "google"
            save_config(cfg, path)

            os.environ["GOOGLE_API_KEY"] = "key-from-shell"
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["OPENPPX_GUI_GROUNDING_PROVIDER"], "google")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "key-from-shell")

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

            os.environ.pop("OPENPPX_PROVIDER", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            bootstrap_env_from_config(path)

        self.assertEqual(os.environ["OPENPPX_PROVIDER"], "google")
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
                    "enabled": False,
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                }
            }
            save_config(cfg, path)

            os.environ.pop("OPENPPX_MCP_SERVERS_JSON", None)
            bootstrap_env_from_config(path)

        raw = os.environ.get("OPENPPX_MCP_SERVERS_JSON")
        self.assertIsNotNone(raw)
        parsed = json.loads(raw or "{}")
        self.assertIn("filesystem", parsed)
        self.assertFalse(parsed["filesystem"]["enabled"])
        self.assertEqual(parsed["filesystem"]["command"], "npx")


if __name__ == "__main__":
    unittest.main()
