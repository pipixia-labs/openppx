"""Persistent config support for sentientagent_v2."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from loguru import logger

from .env_utils import is_enabled
from .provider import default_model_for_provider, normalize_model_name, provider_api_key_env
from .security import normalize_allowlist


_CONFIG_CHANNEL_ORDER: tuple[str, ...] = (
    "local",
    "feishu",
    "telegram",
    "whatsapp",
    "discord",
    "mochat",
    "dingtalk",
    "email",
    "slack",
    "qq",
)


def get_data_dir() -> Path:
    """Return the data directory used by sentientagent_v2."""
    return Path.home() / ".sentientagent_v2"


def get_config_path() -> Path:
    """Return the default config file path."""
    return get_data_dir() / "config.json"


def get_default_workspace_path() -> Path:
    """Return default workspace path used by onboard."""
    return get_data_dir() / "workspace"


def default_config() -> dict[str, Any]:
    """Build default config content."""
    return {
        "agent": {
            "workspace": str(get_default_workspace_path()),
            "builtinSkillsDir": "",
        },
        "providers": {
            "google": {
                "enabled": True,
                "apiKey": "",
                "model": default_model_for_provider("google"),
            },
            "openai": {
                "enabled": False,
                "apiKey": "",
                "model": default_model_for_provider("openai"),
            },
            "openrouter": {
                "enabled": False,
                "apiKey": "",
                "model": default_model_for_provider("openrouter"),
            },
        },
        "session": {
            "dbUrl": "",
        },
        "channels": {
            "local": {
                "enabled": True,
            },
            "feishu": {
                "enabled": False,
                "appId": "",
                "appSecret": "",
                "encryptKey": "",
                "verificationToken": "",
                "allowFrom": [],
            },
            "telegram": {
                "enabled": False,
                "token": "",
                "proxy": "",
                "allowFrom": [],
            },
            "whatsapp": {
                "enabled": False,
                "bridgeUrl": "ws://localhost:3001",
                "bridgeToken": "",
                "allowFrom": [],
                "reconnectDelaySeconds": 5,
            },
            "discord": {
                "enabled": False,
                "token": "",
                "gatewayUrl": "wss://gateway.discord.gg/?v=10&encoding=json",
                "intents": 37377,
                "allowFrom": [],
                "pollChannels": [],
                "pollIntervalSeconds": 10,
                "includeBots": False,
            },
            "mochat": {
                "enabled": False,
                "baseUrl": "https://mochat.io",
                "clawToken": "",
                "agentUserId": "",
                "sessions": [],
                "panels": [],
                "allowFrom": [],
                "pollIntervalSeconds": 5,
                "watchTimeoutMs": 15000,
                "watchLimit": 20,
                "panelLimit": 50,
            },
            "dingtalk": {
                "enabled": False,
                "clientId": "",
                "clientSecret": "",
                "allowFrom": [],
                "streamModeEnabled": True,
                "streamReconnectDelaySeconds": 5,
            },
            "email": {
                "enabled": False,
                "consentGranted": False,
                "imapHost": "",
                "imapPort": 993,
                "imapUsername": "",
                "imapPassword": "",
                "imapMailbox": "INBOX",
                "imapUseSsl": True,
                "smtpHost": "",
                "smtpPort": 587,
                "smtpUsername": "",
                "smtpPassword": "",
                "smtpUseTls": True,
                "smtpUseSsl": False,
                "fromAddress": "",
                "autoReplyEnabled": True,
                "pollIntervalSeconds": 30,
                "markSeen": True,
                "maxBodyChars": 12000,
                "subjectPrefix": "Re: ",
                "allowFrom": [],
            },
            "slack": {
                "enabled": False,
                "mode": "socket",
                "botToken": "",
                "appToken": "",
                "defaultChannel": "",
                "replyInThread": True,
                "reactEmoji": "eyes",
                "groupPolicy": "mention",
                "groupAllowFrom": [],
                "allowFrom": [],
                "pollChannels": [],
                "pollIntervalSeconds": 15,
                "includeBots": False,
                "dm": {
                    "enabled": True,
                    "policy": "open",
                    "allowFrom": [],
                },
            },
            "qq": {
                "enabled": False,
                "appId": "",
                "secret": "",
                "allowFrom": [],
            },
        },
        "web": {
            "enabled": True,
            "search": {
                "enabled": True,
                "provider": "brave",
                "apiKey": "",
                "maxResults": 5,
            },
        },
        "security": {
            "restrictToWorkspace": False,
            "allowExec": True,
            "allowNetwork": True,
            "execAllowlist": [],
        },
        "tools": {
            "mcpServers": {},
        },
        "debug": False,
    }


def _deep_merge(base: Any, override: Any) -> Any:
    """Merge override into base, but keep only keys defined in base schema."""
    if isinstance(base, dict):
        if not isinstance(override, dict):
            return base
        # Empty dict in defaults acts as an extensible map schema.
        if not base:
            return override
        merged: dict[str, Any] = {}
        for key, base_value in base.items():
            merged[key] = _deep_merge(base_value, override.get(key))
        return merged
    return override if override is not None else base


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize external config by filling missing fields with defaults."""
    cfg = _deep_merge(default_config(), raw or {})
    if not isinstance(cfg, dict):
        return default_config()
    return cfg


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load config from disk. Missing/invalid config falls back to defaults."""
    path = config_path or get_config_path()
    if not path.exists():
        return default_config()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Warning: failed to load config at {}: {}", path, exc)
        return default_config()

    if not isinstance(data, dict):
        logger.debug("Warning: invalid config root at {}; expected JSON object", path)
        return default_config()
    return normalize_config(data)


def save_config(config: dict[str, Any], config_path: Path | None = None) -> Path:
    """Save config to disk and return the output path."""
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_config(config)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Best effort: keep local secrets private on POSIX systems.
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _resolve_enabled_channels(channels: dict[str, Any]) -> str:
    """Resolve enabled channel names from per-channel enabled flags."""
    names: list[str] = []
    for name in _CONFIG_CHANNEL_ORDER:
        raw = channels.get(name)
        if isinstance(raw, dict):
            enabled = is_enabled(raw.get("enabled"), default=(name == "local"))
        else:
            enabled = is_enabled(raw, default=False)
        if enabled:
            names.append(name)

    if not names:
        return "local"
    return ",".join(names)


def _resolve_provider(cfg: dict[str, Any]) -> tuple[str, bool, str, str]:
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        providers = {}

    ordered = ("google", "openai", "openrouter")
    enabled_names: list[str] = []
    for name in ordered:
        raw_cfg = providers.get(name, {})
        if not isinstance(raw_cfg, dict):
            raw_cfg = {}
        if is_enabled(raw_cfg.get("enabled"), default=(name == "google")):
            enabled_names.append(name)

    if not enabled_names:
        return "google", False, default_model_for_provider("google"), ""

    active = enabled_names[0]
    active_cfg = providers.get(active, {})
    if not isinstance(active_cfg, dict):
        active_cfg = {}
    model = normalize_model_name(active, active_cfg.get("model"))
    api_key = str(active_cfg.get("apiKey", "")).strip()
    return active, True, model, api_key


def _resolve_web(cfg: dict[str, Any]) -> tuple[bool, bool, str, int, str]:
    web = cfg.get("web")
    if not isinstance(web, dict):
        web = {}
    search = web.get("search")
    if not isinstance(search, dict):
        search = {}

    web_enabled = is_enabled(web.get("enabled"), default=True)
    search_enabled = web_enabled and is_enabled(search.get("enabled"), default=True)
    provider = str(search.get("provider", "brave")).strip().lower() or "brave"

    raw_max = search.get("maxResults", 5)
    try:
        max_results = int(raw_max)
    except Exception:
        max_results = 5
    max_results = min(max(max_results, 1), 10)

    api_key = str(search.get("apiKey", "")).strip()
    return web_enabled, search_enabled, provider, max_results, api_key


def _resolve_security(cfg: dict[str, Any]) -> tuple[bool, bool, bool, str]:
    security = cfg.get("security")
    if not isinstance(security, dict):
        security = {}

    restrict = is_enabled(security.get("restrictToWorkspace"), default=False)
    allow_exec = is_enabled(security.get("allowExec"), default=True)
    allow_network = is_enabled(security.get("allowNetwork"), default=True)

    raw_allowlist = security.get("execAllowlist", [])
    if not isinstance(raw_allowlist, list):
        raw_allowlist = []
    allowlist = ",".join(normalize_allowlist(raw_allowlist))
    return restrict, allow_exec, allow_network, allowlist


def _resolve_mcp_servers_json(cfg: dict[str, Any]) -> str:
    """Serialize configured MCP servers into a stable JSON string."""
    tools = cfg.get("tools")
    if not isinstance(tools, dict):
        return "{}"
    raw = tools.get("mcpServers", {})
    if not isinstance(raw, dict):
        return "{}"
    # Compact form keeps env values readable while preserving full structure.
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))


def config_to_env(config: dict[str, Any]) -> dict[str, str]:
    """Map config payload into runtime environment variables."""
    cfg = normalize_config(config)
    agent = cfg.get("agent", {})
    session = cfg.get("session", {})
    channels = cfg.get("channels", {})
    feishu = channels.get("feishu", {}) if isinstance(channels, dict) else {}
    if not isinstance(feishu, dict):
        feishu = {}
    feishu_allow_from = feishu.get("allowFrom", [])
    if not isinstance(feishu_allow_from, list):
        feishu_allow_from = []
    telegram = channels.get("telegram", {}) if isinstance(channels, dict) else {}
    if not isinstance(telegram, dict):
        telegram = {}
    telegram_allow_from = telegram.get("allowFrom", [])
    if not isinstance(telegram_allow_from, list):
        telegram_allow_from = []
    whatsapp = channels.get("whatsapp", {}) if isinstance(channels, dict) else {}
    if not isinstance(whatsapp, dict):
        whatsapp = {}
    whatsapp_allow_from = whatsapp.get("allowFrom", [])
    if not isinstance(whatsapp_allow_from, list):
        whatsapp_allow_from = []
    discord = channels.get("discord", {}) if isinstance(channels, dict) else {}
    if not isinstance(discord, dict):
        discord = {}
    discord_allow_from = discord.get("allowFrom", [])
    if not isinstance(discord_allow_from, list):
        discord_allow_from = []
    discord_poll_channels = discord.get("pollChannels", [])
    if not isinstance(discord_poll_channels, list):
        discord_poll_channels = []
    mochat = channels.get("mochat", {}) if isinstance(channels, dict) else {}
    if not isinstance(mochat, dict):
        mochat = {}
    mochat_allow_from = mochat.get("allowFrom", [])
    if not isinstance(mochat_allow_from, list):
        mochat_allow_from = []
    mochat_sessions = mochat.get("sessions", [])
    if not isinstance(mochat_sessions, list):
        mochat_sessions = []
    mochat_panels = mochat.get("panels", [])
    if not isinstance(mochat_panels, list):
        mochat_panels = []
    dingtalk = channels.get("dingtalk", {}) if isinstance(channels, dict) else {}
    if not isinstance(dingtalk, dict):
        dingtalk = {}
    dingtalk_allow_from = dingtalk.get("allowFrom", [])
    if not isinstance(dingtalk_allow_from, list):
        dingtalk_allow_from = []
    email = channels.get("email", {}) if isinstance(channels, dict) else {}
    if not isinstance(email, dict):
        email = {}
    email_allow_from = email.get("allowFrom", [])
    if not isinstance(email_allow_from, list):
        email_allow_from = []
    slack = channels.get("slack", {}) if isinstance(channels, dict) else {}
    if not isinstance(slack, dict):
        slack = {}
    slack_allow_from = slack.get("allowFrom", [])
    if not isinstance(slack_allow_from, list):
        slack_allow_from = []
    slack_poll_channels = slack.get("pollChannels", [])
    if not isinstance(slack_poll_channels, list):
        slack_poll_channels = []
    qq = channels.get("qq", {}) if isinstance(channels, dict) else {}
    if not isinstance(qq, dict):
        qq = {}
    qq_allow_from = qq.get("allowFrom", [])
    if not isinstance(qq_allow_from, list):
        qq_allow_from = []
    provider_name, provider_enabled, model, provider_api_key = _resolve_provider(cfg)
    web_enabled, web_search_enabled, web_search_provider, web_search_max_results, web_search_api_key = _resolve_web(
        cfg
    )
    restrict_workspace, allow_exec, allow_network, exec_allowlist = _resolve_security(cfg)
    mcp_servers_json = _resolve_mcp_servers_json(cfg)
    debug = cfg.get("debug", False)

    provider_key_env = provider_api_key_env(provider_name) if provider_enabled else None
    env = {
        "GOOGLE_API_KEY": "",
        "OPENAI_API_KEY": "",
        "OPENROUTER_API_KEY": "",
        "SENTIENTAGENT_V2_MODEL": model,
        "SENTIENTAGENT_V2_PROVIDER": provider_name,
        "SENTIENTAGENT_V2_PROVIDER_ENABLED": "1" if provider_enabled else "0",
        "SENTIENTAGENT_V2_WORKSPACE": str(agent.get("workspace", "")).strip(),
        "SENTIENTAGENT_V2_BUILTIN_SKILLS_DIR": str(agent.get("builtinSkillsDir", "")).strip(),
        "SENTIENTAGENT_V2_SESSION_DB_URL": str(session.get("dbUrl", "")).strip(),
        "SENTIENTAGENT_V2_CHANNELS": _resolve_enabled_channels(channels if isinstance(channels, dict) else {}),
        "FEISHU_APP_ID": str(feishu.get("appId", "")).strip(),
        "FEISHU_APP_SECRET": str(feishu.get("appSecret", "")).strip(),
        "FEISHU_ENCRYPT_KEY": str(feishu.get("encryptKey", "")).strip(),
        "FEISHU_VERIFICATION_TOKEN": str(feishu.get("verificationToken", "")).strip(),
        "FEISHU_ALLOW_FROM": ",".join(normalize_allowlist(feishu_allow_from)),
        "TELEGRAM_BOT_TOKEN": str(telegram.get("token", "")).strip(),
        "TELEGRAM_ALLOW_FROM": ",".join(normalize_allowlist(telegram_allow_from)),
        "TELEGRAM_PROXY": str(telegram.get("proxy", "")).strip(),
        "WHATSAPP_BRIDGE_URL": str(whatsapp.get("bridgeUrl", "")).strip(),
        "WHATSAPP_BRIDGE_TOKEN": str(whatsapp.get("bridgeToken", "")).strip(),
        "WHATSAPP_ALLOW_FROM": ",".join(normalize_allowlist(whatsapp_allow_from)),
        "WHATSAPP_RECONNECT_DELAY_SECONDS": str(whatsapp.get("reconnectDelaySeconds", 5)),
        "DISCORD_BOT_TOKEN": str(discord.get("token", "")).strip(),
        "DISCORD_ALLOW_FROM": ",".join(normalize_allowlist(discord_allow_from)),
        "DISCORD_POLL_CHANNELS": ",".join(normalize_allowlist(discord_poll_channels)),
        "DISCORD_POLL_INTERVAL_SECONDS": str(discord.get("pollIntervalSeconds", 10)),
        "DISCORD_INCLUDE_BOTS": "1" if is_enabled(discord.get("includeBots"), default=False) else "0",
        "MOCHAT_BASE_URL": str(mochat.get("baseUrl", "")).strip(),
        "MOCHAT_CLAW_TOKEN": str(mochat.get("clawToken", "")).strip(),
        "MOCHAT_AGENT_USER_ID": str(mochat.get("agentUserId", "")).strip(),
        "MOCHAT_SESSIONS": ",".join(normalize_allowlist(mochat_sessions)),
        "MOCHAT_PANELS": ",".join(normalize_allowlist(mochat_panels)),
        "MOCHAT_ALLOW_FROM": ",".join(normalize_allowlist(mochat_allow_from)),
        "MOCHAT_POLL_INTERVAL_SECONDS": str(mochat.get("pollIntervalSeconds", 5)),
        "MOCHAT_WATCH_TIMEOUT_MS": str(mochat.get("watchTimeoutMs", 15000)),
        "MOCHAT_WATCH_LIMIT": str(mochat.get("watchLimit", 20)),
        "MOCHAT_PANEL_LIMIT": str(mochat.get("panelLimit", 50)),
        "DINGTALK_CLIENT_ID": str(dingtalk.get("clientId", "")).strip(),
        "DINGTALK_CLIENT_SECRET": str(dingtalk.get("clientSecret", "")).strip(),
        "DINGTALK_ALLOW_FROM": ",".join(normalize_allowlist(dingtalk_allow_from)),
        "DINGTALK_STREAM_MODE_ENABLED": "1"
        if is_enabled(dingtalk.get("streamModeEnabled"), default=True)
        else "0",
        "DINGTALK_STREAM_RECONNECT_DELAY_SECONDS": str(dingtalk.get("streamReconnectDelaySeconds", 5)),
        "EMAIL_CONSENT_GRANTED": "1" if is_enabled(email.get("consentGranted"), default=False) else "0",
        "EMAIL_IMAP_HOST": str(email.get("imapHost", "")).strip(),
        "EMAIL_IMAP_PORT": str(email.get("imapPort", 993)),
        "EMAIL_IMAP_USERNAME": str(email.get("imapUsername", "")).strip(),
        "EMAIL_IMAP_PASSWORD": str(email.get("imapPassword", "")),
        "EMAIL_IMAP_MAILBOX": str(email.get("imapMailbox", "INBOX")).strip() or "INBOX",
        "EMAIL_IMAP_USE_SSL": "1" if is_enabled(email.get("imapUseSsl"), default=True) else "0",
        "EMAIL_SMTP_HOST": str(email.get("smtpHost", "")).strip(),
        "EMAIL_SMTP_PORT": str(email.get("smtpPort", 587)),
        "EMAIL_SMTP_USERNAME": str(email.get("smtpUsername", "")).strip(),
        "EMAIL_SMTP_PASSWORD": str(email.get("smtpPassword", "")),
        "EMAIL_SMTP_USE_TLS": "1" if is_enabled(email.get("smtpUseTls"), default=True) else "0",
        "EMAIL_SMTP_USE_SSL": "1" if is_enabled(email.get("smtpUseSsl"), default=False) else "0",
        "EMAIL_FROM_ADDRESS": str(email.get("fromAddress", "")).strip(),
        "EMAIL_AUTO_REPLY_ENABLED": "1" if is_enabled(email.get("autoReplyEnabled"), default=True) else "0",
        "EMAIL_POLL_INTERVAL_SECONDS": str(email.get("pollIntervalSeconds", 30)),
        "EMAIL_MARK_SEEN": "1" if is_enabled(email.get("markSeen"), default=True) else "0",
        "EMAIL_MAX_BODY_CHARS": str(email.get("maxBodyChars", 12000)),
        "EMAIL_ALLOW_FROM": ",".join(normalize_allowlist(email_allow_from)),
        "SLACK_BOT_TOKEN": str(slack.get("botToken", "")).strip(),
        "SLACK_APP_TOKEN": str(slack.get("appToken", "")).strip(),
        "SLACK_DEFAULT_CHANNEL": str(slack.get("defaultChannel", "")).strip(),
        "SLACK_ALLOW_FROM": ",".join(normalize_allowlist(slack_allow_from)),
        "SLACK_POLL_CHANNELS": ",".join(normalize_allowlist(slack_poll_channels)),
        "SLACK_POLL_INTERVAL_SECONDS": str(slack.get("pollIntervalSeconds", 15)),
        "SLACK_INCLUDE_BOTS": "1" if is_enabled(slack.get("includeBots"), default=False) else "0",
        "QQ_APP_ID": str(qq.get("appId", "")).strip(),
        "QQ_SECRET": str(qq.get("secret", "")).strip(),
        "QQ_ALLOW_FROM": ",".join(normalize_allowlist(qq_allow_from)),
        "BRAVE_API_KEY": web_search_api_key,
        "SENTIENTAGENT_V2_WEB_ENABLED": "1" if web_enabled else "0",
        "SENTIENTAGENT_V2_WEB_SEARCH_ENABLED": "1" if web_search_enabled else "0",
        "SENTIENTAGENT_V2_WEB_SEARCH_PROVIDER": web_search_provider,
        "SENTIENTAGENT_V2_WEB_SEARCH_MAX_RESULTS": str(web_search_max_results),
        "SENTIENTAGENT_V2_RESTRICT_TO_WORKSPACE": "1" if restrict_workspace else "0",
        "SENTIENTAGENT_V2_ALLOW_EXEC": "1" if allow_exec else "0",
        "SENTIENTAGENT_V2_ALLOW_NETWORK": "1" if allow_network else "0",
        "SENTIENTAGENT_V2_EXEC_ALLOWLIST": exec_allowlist,
        "SENTIENTAGENT_V2_MCP_SERVERS_JSON": mcp_servers_json,
        "SENTIENTAGENT_V2_DEBUG": "1" if bool(debug) else "0",
    }
    if provider_key_env:
        env[provider_key_env] = provider_api_key
    return env


def _managed_env_keys() -> set[str]:
    """Keys controlled by config-to-env mapping."""
    return set(config_to_env(default_config()).keys())


def apply_config_to_env(
    config: dict[str, Any],
    *,
    overwrite: bool = False,
    clear_missing: bool = False,
) -> None:
    """Inject config fields into environment variables."""
    mapped = config_to_env(config)
    if clear_missing:
        for key in _managed_env_keys():
            if key not in mapped:
                os.environ.pop(key, None)

    for key, value in mapped.items():
        if not value and key != "SENTIENTAGENT_V2_DEBUG":
            if clear_missing:
                os.environ.pop(key, None)
            continue
        if overwrite or key not in os.environ:
            os.environ[key] = value


def bootstrap_env_from_config(config_path: Path | None = None) -> dict[str, Any] | None:
    """Load config file (if present) and apply values to process env."""
    path = config_path or get_config_path()
    if not path.exists():
        return None
    cfg = load_config(path)
    # Config file is the source of truth for runtime bootstrap.
    apply_config_to_env(cfg, overwrite=True, clear_missing=True)
    return deepcopy(cfg)
