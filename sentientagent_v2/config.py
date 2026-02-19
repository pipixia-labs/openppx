"""Persistent config support for sentientagent_v2."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from loguru import logger

from .env_utils import is_enabled
from .provider import (
    DEFAULT_PROVIDER,
    default_model_for_provider,
    normalize_model_name,
    provider_api_key_env,
    provider_api_key_env_keys,
    provider_default_api_base,
    provider_names,
)
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

# (channel_name, config_key, env_key)
_CHANNEL_STRIPPED_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("feishu", "appId", "FEISHU_APP_ID"),
    ("feishu", "appSecret", "FEISHU_APP_SECRET"),
    ("feishu", "encryptKey", "FEISHU_ENCRYPT_KEY"),
    ("feishu", "verificationToken", "FEISHU_VERIFICATION_TOKEN"),
    ("telegram", "token", "TELEGRAM_BOT_TOKEN"),
    ("telegram", "proxy", "TELEGRAM_PROXY"),
    ("whatsapp", "bridgeUrl", "WHATSAPP_BRIDGE_URL"),
    ("whatsapp", "bridgeToken", "WHATSAPP_BRIDGE_TOKEN"),
    ("discord", "token", "DISCORD_BOT_TOKEN"),
    ("mochat", "baseUrl", "MOCHAT_BASE_URL"),
    ("mochat", "clawToken", "MOCHAT_CLAW_TOKEN"),
    ("mochat", "agentUserId", "MOCHAT_AGENT_USER_ID"),
    ("dingtalk", "clientId", "DINGTALK_CLIENT_ID"),
    ("dingtalk", "clientSecret", "DINGTALK_CLIENT_SECRET"),
    ("email", "imapHost", "EMAIL_IMAP_HOST"),
    ("email", "imapUsername", "EMAIL_IMAP_USERNAME"),
    ("email", "smtpHost", "EMAIL_SMTP_HOST"),
    ("email", "smtpUsername", "EMAIL_SMTP_USERNAME"),
    ("email", "fromAddress", "EMAIL_FROM_ADDRESS"),
    ("slack", "botToken", "SLACK_BOT_TOKEN"),
    ("slack", "appToken", "SLACK_APP_TOKEN"),
    ("slack", "defaultChannel", "SLACK_DEFAULT_CHANNEL"),
    ("qq", "appId", "QQ_APP_ID"),
    ("qq", "secret", "QQ_SECRET"),
)

# (channel_name, config_key, env_key) values are stringified without trim.
_CHANNEL_RAW_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("email", "imapPassword", "EMAIL_IMAP_PASSWORD"),
    ("email", "smtpPassword", "EMAIL_SMTP_PASSWORD"),
)

# (channel_name, config_key, env_key)
_CHANNEL_ALLOWLIST_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("feishu", "allowFrom", "FEISHU_ALLOW_FROM"),
    ("telegram", "allowFrom", "TELEGRAM_ALLOW_FROM"),
    ("whatsapp", "allowFrom", "WHATSAPP_ALLOW_FROM"),
    ("discord", "allowFrom", "DISCORD_ALLOW_FROM"),
    ("discord", "pollChannels", "DISCORD_POLL_CHANNELS"),
    ("mochat", "sessions", "MOCHAT_SESSIONS"),
    ("mochat", "panels", "MOCHAT_PANELS"),
    ("mochat", "allowFrom", "MOCHAT_ALLOW_FROM"),
    ("dingtalk", "allowFrom", "DINGTALK_ALLOW_FROM"),
    ("email", "allowFrom", "EMAIL_ALLOW_FROM"),
    ("slack", "allowFrom", "SLACK_ALLOW_FROM"),
    ("slack", "pollChannels", "SLACK_POLL_CHANNELS"),
    ("qq", "allowFrom", "QQ_ALLOW_FROM"),
)

# (channel_name, config_key, env_key, default)
_CHANNEL_FLAG_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("discord", "includeBots", "DISCORD_INCLUDE_BOTS", False),
    ("dingtalk", "streamModeEnabled", "DINGTALK_STREAM_MODE_ENABLED", True),
    ("email", "consentGranted", "EMAIL_CONSENT_GRANTED", False),
    ("email", "imapUseSsl", "EMAIL_IMAP_USE_SSL", True),
    ("email", "smtpUseTls", "EMAIL_SMTP_USE_TLS", True),
    ("email", "smtpUseSsl", "EMAIL_SMTP_USE_SSL", False),
    ("email", "autoReplyEnabled", "EMAIL_AUTO_REPLY_ENABLED", True),
    ("email", "markSeen", "EMAIL_MARK_SEEN", True),
    ("slack", "includeBots", "SLACK_INCLUDE_BOTS", False),
)

# (channel_name, config_key, env_key, default)
_CHANNEL_DEFAULT_VALUE_FIELDS: tuple[tuple[str, str, str, Any], ...] = (
    ("whatsapp", "reconnectDelaySeconds", "WHATSAPP_RECONNECT_DELAY_SECONDS", 5),
    ("discord", "pollIntervalSeconds", "DISCORD_POLL_INTERVAL_SECONDS", 10),
    ("mochat", "pollIntervalSeconds", "MOCHAT_POLL_INTERVAL_SECONDS", 5),
    ("mochat", "watchTimeoutMs", "MOCHAT_WATCH_TIMEOUT_MS", 15000),
    ("mochat", "watchLimit", "MOCHAT_WATCH_LIMIT", 20),
    ("mochat", "panelLimit", "MOCHAT_PANEL_LIMIT", 50),
    ("dingtalk", "streamReconnectDelaySeconds", "DINGTALK_STREAM_RECONNECT_DELAY_SECONDS", 5),
    ("email", "imapPort", "EMAIL_IMAP_PORT", 993),
    ("email", "smtpPort", "EMAIL_SMTP_PORT", 587),
    ("email", "pollIntervalSeconds", "EMAIL_POLL_INTERVAL_SECONDS", 30),
    ("email", "maxBodyChars", "EMAIL_MAX_BODY_CHARS", 12000),
    ("slack", "pollIntervalSeconds", "SLACK_POLL_INTERVAL_SECONDS", 15),
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
            name: {
                "enabled": name == DEFAULT_PROVIDER,
                "apiKey": "",
                "model": default_model_for_provider(name),
                "apiBase": provider_default_api_base(name),
                "extraHeaders": {},
            }
            for name in provider_names()
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


def _resolve_provider(cfg: dict[str, Any]) -> tuple[str, bool, str, str, str, str]:
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        providers = {}

    ordered = provider_names()
    enabled_names: list[str] = []
    for name in ordered:
        raw_cfg = providers.get(name, {})
        if not isinstance(raw_cfg, dict):
            raw_cfg = {}
        if is_enabled(raw_cfg.get("enabled"), default=(name == DEFAULT_PROVIDER)):
            enabled_names.append(name)

    if not enabled_names:
        default_base = provider_default_api_base(DEFAULT_PROVIDER)
        return DEFAULT_PROVIDER, False, default_model_for_provider(DEFAULT_PROVIDER), "", default_base, ""

    active = enabled_names[0]
    active_cfg = providers.get(active, {})
    if not isinstance(active_cfg, dict):
        active_cfg = {}
    model = normalize_model_name(active, active_cfg.get("model"))
    api_key = str(active_cfg.get("apiKey", "")).strip()
    api_base = str(active_cfg.get("apiBase", "")).strip() or provider_default_api_base(active)
    extra_headers = active_cfg.get("extraHeaders", {})
    if not isinstance(extra_headers, dict):
        extra_headers = {}
    extra_headers_json = json.dumps(extra_headers, ensure_ascii=False, separators=(",", ":")) if extra_headers else ""
    return active, True, model, api_key, api_base, extra_headers_json


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


def _as_dict(value: Any) -> dict[str, Any]:
    """Return mapping value as dict, otherwise an empty dict."""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """Return sequence value as list, otherwise an empty list."""
    return value if isinstance(value, list) else []


def _channel_config(channels: dict[str, Any], name: str) -> dict[str, Any]:
    """Read one channel section as dict with safe fallback."""
    return _as_dict(channels.get(name))


def _channel_sections(channels: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalize per-channel config payloads into a name->dict mapping."""
    return {name: _channel_config(channels, name) for name in _CONFIG_CHANNEL_ORDER}


def _channel_env_values(channels: dict[str, Any]) -> dict[str, str]:
    """Build all channel-related environment variables from config."""
    sections = _channel_sections(channels)
    env: dict[str, str] = {}

    for channel_name, cfg_key, env_key in _CHANNEL_STRIPPED_FIELDS:
        env[env_key] = str(sections[channel_name].get(cfg_key, "")).strip()

    for channel_name, cfg_key, env_key in _CHANNEL_RAW_FIELDS:
        env[env_key] = str(sections[channel_name].get(cfg_key, ""))

    for channel_name, cfg_key, env_key in _CHANNEL_ALLOWLIST_FIELDS:
        env[env_key] = ",".join(normalize_allowlist(_as_list(sections[channel_name].get(cfg_key))))

    for channel_name, cfg_key, env_key, default in _CHANNEL_FLAG_FIELDS:
        env[env_key] = "1" if is_enabled(sections[channel_name].get(cfg_key), default=default) else "0"

    for channel_name, cfg_key, env_key, default in _CHANNEL_DEFAULT_VALUE_FIELDS:
        env[env_key] = str(sections[channel_name].get(cfg_key, default))

    email = sections["email"]
    env["EMAIL_IMAP_MAILBOX"] = str(email.get("imapMailbox", "INBOX")).strip() or "INBOX"
    return env


def config_to_env(config: dict[str, Any]) -> dict[str, str]:
    """Map config payload into runtime environment variables."""
    cfg = normalize_config(config)
    agent = _as_dict(cfg.get("agent"))
    session = _as_dict(cfg.get("session"))
    channels = _as_dict(cfg.get("channels"))
    channel_env = _channel_env_values(channels)
    provider_name, provider_enabled, model, provider_api_key, provider_api_base, provider_extra_headers = _resolve_provider(
        cfg
    )
    web_enabled, web_search_enabled, web_search_provider, web_search_max_results, web_search_api_key = _resolve_web(
        cfg
    )
    restrict_workspace, allow_exec, allow_network, exec_allowlist = _resolve_security(cfg)
    mcp_servers_json = _resolve_mcp_servers_json(cfg)
    debug = cfg.get("debug", False)

    provider_key_env = provider_api_key_env(provider_name) if provider_enabled else None
    env = {
        **{env_key: "" for env_key in provider_api_key_env_keys()},
        "SENTIENTAGENT_V2_MODEL": model,
        "SENTIENTAGENT_V2_PROVIDER": provider_name,
        "SENTIENTAGENT_V2_PROVIDER_ENABLED": "1" if provider_enabled else "0",
        "SENTIENTAGENT_V2_PROVIDER_API_BASE": provider_api_base,
        "SENTIENTAGENT_V2_PROVIDER_EXTRA_HEADERS_JSON": provider_extra_headers,
        "SENTIENTAGENT_V2_WORKSPACE": str(agent.get("workspace", "")).strip(),
        "SENTIENTAGENT_V2_BUILTIN_SKILLS_DIR": str(agent.get("builtinSkillsDir", "")).strip(),
        "SENTIENTAGENT_V2_SESSION_DB_URL": str(session.get("dbUrl", "")).strip(),
        "SENTIENTAGENT_V2_CHANNELS": _resolve_enabled_channels(channels),
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
    env.update(channel_env)
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
