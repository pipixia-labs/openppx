"""Tests for channel factory configuration logic."""

from __future__ import annotations

import os
import unittest

from openpipixia.bus.queue import MessageBus
from openpipixia.channels.factory import (
    build_channel_manager,
    parse_enabled_channels,
    validate_channel_setup,
)


class ChannelFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_parse_enabled_channels_dedup(self) -> None:
        names = parse_enabled_channels("local,feishu,local")
        self.assertEqual(names, ["local", "feishu"])

    def test_parse_enabled_channels_empty_falls_back_to_local(self) -> None:
        names = parse_enabled_channels("")
        self.assertEqual(names, ["local"])

    def test_validate_reports_unknown(self) -> None:
        issues = validate_channel_setup(["unknown"])
        self.assertTrue(any("Unsupported channels" in item for item in issues))

    def test_validate_reports_known_but_unimplemented_channel(self) -> None:
        issues = validate_channel_setup(["telegram"])
        self.assertTrue(any("Missing TELEGRAM_BOT_TOKEN" in item for item in issues))
        self.assertFalse(any("Unsupported channels" in item for item in issues))

    def test_validate_reports_email_setup_issues(self) -> None:
        issues = validate_channel_setup(["email"])
        self.assertTrue(any("EMAIL_CONSENT_GRANTED" in item for item in issues))
        self.assertTrue(any("EMAIL_SMTP_HOST" in item for item in issues))
        self.assertFalse(any("Unsupported channels" in item for item in issues))

    def test_validate_reports_slack_setup_issues(self) -> None:
        issues = validate_channel_setup(["slack"])
        self.assertTrue(any("SLACK_BOT_TOKEN" in item for item in issues))
        self.assertFalse(any("Unsupported channels" in item for item in issues))

    def test_validate_reports_qq_setup_issues(self) -> None:
        issues = validate_channel_setup(["qq"])
        self.assertTrue(any("QQ_APP_ID" in item for item in issues))
        self.assertTrue(any("QQ_SECRET" in item for item in issues))
        self.assertFalse(any("Unsupported channels" in item for item in issues))

    def test_validate_reports_discord_setup_issues(self) -> None:
        issues = validate_channel_setup(["discord"])
        self.assertTrue(any("DISCORD_BOT_TOKEN" in item for item in issues))
        self.assertFalse(any("Unsupported channels" in item for item in issues))

    def test_validate_reports_dingtalk_setup_issues(self) -> None:
        issues = validate_channel_setup(["dingtalk"])
        self.assertTrue(any("DINGTALK_CLIENT_ID" in item for item in issues))
        self.assertTrue(any("DINGTALK_CLIENT_SECRET" in item for item in issues))
        self.assertFalse(any("Unsupported channels" in item for item in issues))

    def test_validate_reports_whatsapp_setup_issues(self) -> None:
        issues = validate_channel_setup(["whatsapp"])
        self.assertTrue(any("WHATSAPP_BRIDGE_URL" in item for item in issues))
        self.assertFalse(any("Unsupported channels" in item for item in issues))

    def test_build_local_channel_manager(self) -> None:
        manager, local_channel = build_channel_manager(bus=MessageBus(), channel_names=["local"])
        self.assertIsNotNone(local_channel)
        self.assertIn("local", manager.channels)

    def test_build_manager_skips_unimplemented_channel(self) -> None:
        os.environ["TELEGRAM_BOT_TOKEN"] = "token-1"
        manager, local_channel = build_channel_manager(bus=MessageBus(), channel_names=["local", "telegram"])
        self.assertIsNotNone(local_channel)
        self.assertIn("local", manager.channels)
        self.assertIn("telegram", manager.channels)

    def test_build_manager_registers_email_when_configured(self) -> None:
        os.environ["EMAIL_CONSENT_GRANTED"] = "1"
        os.environ["EMAIL_SMTP_HOST"] = "smtp.example.com"
        os.environ["EMAIL_SMTP_USERNAME"] = "bot@example.com"
        os.environ["EMAIL_SMTP_PASSWORD"] = "pw"
        manager, _ = build_channel_manager(bus=MessageBus(), channel_names=["email"])
        self.assertIn("email", manager.channels)

    def test_build_manager_registers_slack_when_configured(self) -> None:
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-token"
        manager, _ = build_channel_manager(bus=MessageBus(), channel_names=["slack"])
        self.assertIn("slack", manager.channels)

    def test_build_manager_registers_qq_when_configured(self) -> None:
        os.environ["QQ_APP_ID"] = "app-id"
        os.environ["QQ_SECRET"] = "app-secret"
        manager, _ = build_channel_manager(bus=MessageBus(), channel_names=["qq"])
        self.assertIn("qq", manager.channels)

    def test_build_manager_registers_discord_when_configured(self) -> None:
        os.environ["DISCORD_BOT_TOKEN"] = "discord-token"
        manager, _ = build_channel_manager(bus=MessageBus(), channel_names=["discord"])
        self.assertIn("discord", manager.channels)

    def test_build_manager_registers_dingtalk_when_configured(self) -> None:
        os.environ["DINGTALK_CLIENT_ID"] = "dt-app-id"
        os.environ["DINGTALK_CLIENT_SECRET"] = "dt-app-secret"
        manager, _ = build_channel_manager(bus=MessageBus(), channel_names=["dingtalk"])
        self.assertIn("dingtalk", manager.channels)

    def test_build_manager_registers_whatsapp_when_configured(self) -> None:
        os.environ["WHATSAPP_BRIDGE_URL"] = "ws://127.0.0.1:3001"
        manager, _ = build_channel_manager(bus=MessageBus(), channel_names=["whatsapp"])
        self.assertIn("whatsapp", manager.channels)

if __name__ == "__main__":
    unittest.main()
