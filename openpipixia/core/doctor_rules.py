"""Shared rule metadata for doctor/install config backfill flows."""

from __future__ import annotations

from dataclasses import dataclass

LEGACY_PROVIDER_FIELD_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("api_key", "apiKey"),
    ("api_base", "apiBase"),
)

LEGACY_CHANNEL_FIELD_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("feishu", "app_id", "appId"),
    ("feishu", "app_secret", "appSecret"),
    ("telegram", "bot_token", "token"),
    ("discord", "bot_token", "token"),
    ("dingtalk", "client_id", "clientId"),
    ("dingtalk", "client_secret", "clientSecret"),
    ("slack", "bot_token", "botToken"),
    ("whatsapp", "bridge_url", "bridgeUrl"),
    ("email", "smtp_host", "smtpHost"),
    ("email", "smtp_username", "smtpUsername"),
    ("email", "smtp_password", "smtpPassword"),
    ("qq", "app_id", "appId"),
)

CHANNEL_ENV_BACKFILL_MAPPINGS: tuple[tuple[str, str, str], ...] = (
    ("feishu", "appId", "FEISHU_APP_ID"),
    ("feishu", "appSecret", "FEISHU_APP_SECRET"),
    ("telegram", "token", "TELEGRAM_BOT_TOKEN"),
    ("discord", "token", "DISCORD_BOT_TOKEN"),
    ("dingtalk", "clientId", "DINGTALK_CLIENT_ID"),
    ("dingtalk", "clientSecret", "DINGTALK_CLIENT_SECRET"),
    ("slack", "botToken", "SLACK_BOT_TOKEN"),
    ("whatsapp", "bridgeUrl", "WHATSAPP_BRIDGE_URL"),
    ("email", "smtpHost", "EMAIL_SMTP_HOST"),
    ("email", "smtpUsername", "EMAIL_SMTP_USERNAME"),
    ("email", "smtpPassword", "EMAIL_SMTP_PASSWORD"),
    ("qq", "appId", "QQ_APP_ID"),
    ("qq", "secret", "QQ_SECRET"),
)


@dataclass(frozen=True)
class DoctorChannelEnvBackfillRule:
    """Structured metadata for one doctor channel env-backfill rule."""

    channel: str
    key: str
    env_name: str
    rule: str = "channel_env_backfill"
    code_applied: str = "channel.env.backfilled"
    code_disabled: str = "channel.env.channel_disabled"
    code_target_set: str = "channel.env.target_already_set"
    code_source_missing: str = "channel.env.source_missing"

    @property
    def target_path(self) -> str:
        return f"channels.{self.channel}.{self.key}"


@dataclass(frozen=True)
class DoctorChannelBoolEnvBackfillRule:
    """Structured metadata for one doctor boolean env-backfill rule."""

    channel: str
    key: str
    env_name: str
    rule: str = "email_consent_backfill"
    code_applied: str = "email.consent.backfilled"
    code_present_not_truthy: str = "email.consent.present_not_truthy"
    code_source_missing: str = "email.consent.source_missing"
    truthy_values: tuple[str, ...] = ("1", "true", "yes", "on")

    @property
    def target_path(self) -> str:
        return f"channels.{self.channel}.{self.key}"


def build_doctor_channel_env_backfill_rules() -> tuple[DoctorChannelEnvBackfillRule, ...]:
    """Build doctor channel env-backfill rules from shared channel env mappings."""

    return tuple(
        DoctorChannelEnvBackfillRule(channel=channel, key=key, env_name=env_name)
        for channel, key, env_name in CHANNEL_ENV_BACKFILL_MAPPINGS
    )


DOCTOR_CHANNEL_ENV_BACKFILL_RULES: tuple[DoctorChannelEnvBackfillRule, ...] = build_doctor_channel_env_backfill_rules()

DOCTOR_CHANNEL_BOOL_ENV_BACKFILL_RULES: tuple[DoctorChannelBoolEnvBackfillRule, ...] = (
    DoctorChannelBoolEnvBackfillRule(
        channel="email",
        key="consentGranted",
        env_name="EMAIL_CONSENT_GRANTED",
    ),
)
