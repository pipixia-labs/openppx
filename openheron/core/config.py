"""Persistent config support for openheron."""

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

_EXTENSIBLE_MAP_KEYS: frozenset[str] = frozenset({"env"})


def get_data_dir() -> Path:
    """Return the data directory used by openheron."""
    return Path.home() / ".openheron"


def get_config_path() -> Path:
    """Return the default config file path."""
    return get_data_dir() / "config.json"


def get_runtime_config_path() -> Path:
    """Return the default runtime config path for advanced env overrides."""
    return get_data_dir() / "runtime.json"


def get_agent_home_path(agent_id: str = "main") -> Path:
    """Return one agent home path under ``~/.openheron/agents/<agentId>``."""
    normalized = str(agent_id).strip() or "main"
    return get_data_dir() / "agents" / normalized


def get_default_workspace_path(agent_id: str = "main") -> Path:
    """Return default per-agent workspace path used by install initialization."""
    return get_agent_home_path(agent_id) / "workspace"


def _default_runtime_env_overrides() -> dict[str, Any]:
    """Return default runtime env overrides shown in generated config.

    These values cover runtime knobs that are primarily consumed from
    environment variables. Keeping them in config makes defaults explicit and
    easy to edit.
    """
    return {
        "OPENHERON_MEMORY_ENABLED": True,
        "OPENHERON_MEMORY_BACKEND": "markdown",
        # Keep empty by default so runtime can resolve per-agent memory roots.
        "OPENHERON_MEMORY_MARKDOWN_DIR": "",
        "OPENHERON_COMPACTION_ENABLED": True,
        "OPENHERON_COMPACTION_INTERVAL": 8,
        "OPENHERON_COMPACTION_OVERLAP": 1,
        "OPENHERON_COMPACTION_TOKEN_THRESHOLD": "",
        "OPENHERON_COMPACTION_EVENT_RETENTION": "",
        "OPENHERON_BOOTSTRAP_MAX_CHARS_PER_FILE": 12000,
        "OPENHERON_BOOTSTRAP_MAX_TOTAL_CHARS": 30000,
        "OPENHERON_SUBAGENT_MAX_CONCURRENCY": 2,
        "OPENHERON_MCP_REQUIRED_SERVERS": "",
        "OPENHERON_MCP_PROBE_RETRY_ATTEMPTS": 2,
        "OPENHERON_MCP_PROBE_RETRY_BACKOFF_SECONDS": 0.3,
        "OPENHERON_MCP_DOCTOR_TIMEOUT_SECONDS": 5,
        "OPENHERON_MCP_GATEWAY_TIMEOUT_SECONDS": 5,
        "OPENHERON_WHATSAPP_BRIDGE_PRECHECK": True,
        "OPENHERON_WHATSAPP_BRIDGE_SOURCE": "",
        "OPENHERON_DEBUG_MAX_CHARS": 2000,
    }


def default_config() -> dict[str, Any]:
    """Build default config content."""
    return {
        "agent": {
            "workspace": str(get_default_workspace_path()),
            "builtinSkillsDir": "",
            "heartbeat": {
                "every": "30m",
                "prompt": "",
                "ackMaxChars": 300,
                "showOk": False,
                "showAlerts": True,
                "target": "last",
                "targetChannel": "",
                "targetChatId": "",
                "activeHours": {
                    "start": "",
                    "end": "",
                    "timezone": "user",
                },
            },
        },
        "agents": {
            "defaults": {
                "workspace": str(get_data_dir() / "agents" / "{agentId}" / "workspace"),
                "agentDir": str(get_data_dir() / "agents" / "{agentId}"),
                "security": {
                    "restrictToWorkspace": False,
                    "allowExec": True,
                    "allowNetwork": True,
                    "execAllowlist": [],
                },
                "fs": {
                    "workspaceOnly": False,
                    "allowedPaths": [],
                    "denyPaths": [],
                    "readOnlyPaths": [],
                },
                "tools": {
                    "allow": [],
                    "deny": [],
                },
                "systemPermissions": {
                    "browser": True,
                    "gui": True,
                    "screenshot": True,
                },
            },
            "list": [
                {
                    "id": "main",
                    "default": True,
                    "workspace": str(get_default_workspace_path("main")),
                    "agentDir": str(get_agent_home_path("main")),
                    "skills": [],
                }
            ],
        },
        "bindings": [],
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
        "multimodalProviders": {},
        "gui": {
            "groundingProvider": "",
            "plannerProvider": "",
            "builtinToolsEnabled": True,
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


def default_runtime_config() -> dict[str, Any]:
    """Build advanced runtime config content."""
    return {
        # Optional explicit runtime env overrides. Any key here will be mapped
        # to process env during bootstrap and takes precedence over shell env.
        "env": _default_runtime_env_overrides(),
    }


def _deep_merge(base: Any, override: Any, *, path: tuple[str, ...] = ()) -> Any:
    """Merge override into base, but keep only keys defined in base schema."""
    if isinstance(base, dict):
        if not isinstance(override, dict):
            return base
        # Empty dict in defaults acts as an extensible map schema.
        if not base:
            return override
        merged: dict[str, Any] = {}
        for key, base_value in base.items():
            merged[key] = _deep_merge(base_value, override.get(key), path=(*path, key))
        if path and path[-1] in _EXTENSIBLE_MAP_KEYS:
            for key, value in override.items():
                if key in merged:
                    continue
                merged[key] = value
        return merged
    return override if override is not None else base


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize external config by filling missing fields with defaults."""
    cfg = _deep_merge(default_config(), raw or {})
    if not isinstance(cfg, dict):
        return default_config()
    return cfg


def normalize_runtime_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize runtime config by filling missing fields with defaults."""
    cfg = _deep_merge(default_runtime_config(), raw or {})
    if not isinstance(cfg, dict):
        return default_runtime_config()
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


def load_runtime_config(runtime_config_path: Path | None = None) -> dict[str, Any]:
    """Load runtime config from disk. Missing/invalid config falls back to defaults."""
    path = runtime_config_path or get_runtime_config_path()
    if not path.exists():
        return default_runtime_config()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Warning: failed to load runtime config at {}: {}", path, exc)
        return default_runtime_config()

    if not isinstance(data, dict):
        logger.debug("Warning: invalid runtime config root at {}; expected JSON object", path)
        return default_runtime_config()
    return normalize_runtime_config(data)


def _runtime_config_path_for_config_path(config_path: Path) -> Path:
    """Resolve sibling runtime config path for one config file path."""
    return config_path.with_name("runtime.json")


def save_config(config: dict[str, Any], config_path: Path | None = None) -> Path:
    """Save config to disk and return the output path."""
    path = config_path or get_config_path()
    config_to_write = deepcopy(config)
    legacy_env = config_to_write.pop("env", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_config(config_to_write)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Best effort: keep local secrets private on POSIX systems.
    try:
        path.chmod(0o600)
    except OSError:
        pass

    # Backward compatibility: migrate legacy config.json `env` into runtime.json.
    if isinstance(legacy_env, dict):
        runtime_path = _runtime_config_path_for_config_path(path)
        existing_runtime = load_runtime_config(runtime_config_path=runtime_path)
        merged_runtime_env = {}
        raw_existing_env = existing_runtime.get("env")
        if isinstance(raw_existing_env, dict):
            merged_runtime_env.update(raw_existing_env)
        merged_runtime_env.update(legacy_env)
        save_runtime_config({"env": merged_runtime_env}, runtime_config_path=runtime_path)

    return path


def save_runtime_config(config: dict[str, Any], runtime_config_path: Path | None = None) -> Path:
    """Save runtime config to disk and return the output path."""
    path = runtime_config_path or get_runtime_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_runtime_config(config)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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


def _resolve_default_agent_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve default agent entry from ``agents.list`` with safe fallback."""
    agents = _as_dict(cfg.get("agents"))
    entries = _as_list(agents.get("list"))
    picked: dict[str, Any] | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if picked is None:
            picked = entry
        if bool(entry.get("default")):
            picked = entry
            break
    if isinstance(picked, dict):
        return picked
    return _as_dict(cfg.get("agent"))


def _resolve_default_agent_dir(cfg: dict[str, Any]) -> str:
    """Resolve default agentDir from agents config with fallback."""
    default_agent = _resolve_default_agent_config(cfg)
    candidate = str(default_agent.get("agentDir", "")).strip()
    if candidate:
        agent_id = str(default_agent.get("id", "")).strip() or "main"
        return _expand_agent_path_template(candidate, agent_id=agent_id)
    return str(get_agent_home_path("main"))


def _expand_agent_path_template(raw: str, *, agent_id: str) -> str:
    """Expand ``{agentId}``/``{agent_id}`` placeholders in one path string."""
    text = str(raw).strip()
    if not text:
        return ""
    return text.replace("{agentId}", agent_id).replace("{agent_id}", agent_id)


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


def _resolve_gui_provider_env(cfg: dict[str, Any], *, provider_name: str) -> tuple[str, str, str]:
    """Resolve one multimodal provider into (model, api_key, api_base)."""
    name = str(provider_name).strip()
    if not name:
        return "", "", ""

    providers = cfg.get("multimodalProviders")
    if not isinstance(providers, dict):
        return "", "", ""
    raw = providers.get(name, {})
    if not isinstance(raw, dict):
        return "", "", ""
    if not is_enabled(raw.get("enabled"), default=False):
        return "", "", ""

    model = str(raw.get("model", "")).strip()
    api_key = str(raw.get("apiKey", "")).strip()
    api_base = str(raw.get("apiBase", "")).strip()
    return model, api_key, api_base


def _resolve_gui_multimodal_env(cfg: dict[str, Any]) -> dict[str, str]:
    """Resolve GUI grounding/planner env values from multimodalProviders config."""
    gui = cfg.get("gui")
    if not isinstance(gui, dict):
        gui = {}

    grounding_model, grounding_api_key, grounding_api_base = _resolve_gui_provider_env(
        cfg,
        provider_name=str(gui.get("groundingProvider", "")),
    )
    planner_model, planner_api_key, planner_api_base = _resolve_gui_provider_env(
        cfg,
        provider_name=str(gui.get("plannerProvider", "")),
    )

    return {
        "OPENHERON_GUI_MODEL": grounding_model,
        "OPENHERON_GUI_API_KEY": grounding_api_key,
        "OPENHERON_GUI_BASE_URL": grounding_api_base,
        "OPENHERON_GUI_PLANNER_MODEL": planner_model,
        "OPENHERON_GUI_PLANNER_API_KEY": planner_api_key,
        "OPENHERON_GUI_PLANNER_BASE_URL": planner_api_base,
    }


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


def _stringify_env_override(value: Any) -> str:
    """Convert config `env` values into process-environment string values."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _env_overrides_from_mapping(raw: Any) -> dict[str, str]:
    """Read optional env override mapping from one mapping payload.

    Runtime overrides live in `runtime.json` by default.
    Legacy `config.json.env` is still supported for backward compatibility.
    """
    if not isinstance(raw, dict):
        return {}

    overrides: dict[str, str] = {}
    for key, value in raw.items():
        env_key = str(key).strip()
        if not env_key:
            continue
        overrides[env_key] = _stringify_env_override(value)
    return overrides


def _env_overrides(cfg: dict[str, Any]) -> dict[str, str]:
    """Read legacy env override mapping from config payload."""
    return _env_overrides_from_mapping(cfg.get("env"))


def _coerce_nonnegative_int(value: Any, default: int) -> int:
    """Convert value into a non-negative integer with fallback."""
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(0, parsed)


def config_to_env(
    config: dict[str, Any],
    *,
    runtime_env_overrides: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Map config payload into runtime environment variables."""
    cfg = normalize_config(config)
    agent = _as_dict(cfg.get("agent"))
    default_agent = _resolve_default_agent_config(cfg)
    heartbeat = _as_dict(agent.get("heartbeat"))
    active_hours = _as_dict(heartbeat.get("activeHours"))
    gui = _as_dict(cfg.get("gui"))
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
    gui_multimodal_env = _resolve_gui_multimodal_env(cfg)
    debug = cfg.get("debug", False)

    provider_key_env = provider_api_key_env(provider_name) if provider_enabled else None
    default_agent_id = str(default_agent.get("id", "")).strip() or "main"
    workspace_value = _expand_agent_path_template(
        str(default_agent.get("workspace", agent.get("workspace", ""))).strip(),
        agent_id=default_agent_id,
    )
    env = {
        **{env_key: "" for env_key in provider_api_key_env_keys()},
        "OPENHERON_MODEL": model,
        "OPENHERON_PROVIDER": provider_name,
        "OPENHERON_PROVIDER_ENABLED": "1" if provider_enabled else "0",
        "OPENHERON_PROVIDER_API_BASE": provider_api_base,
        "OPENHERON_PROVIDER_EXTRA_HEADERS_JSON": provider_extra_headers,
        "OPENHERON_WORKSPACE": workspace_value,
        "OPENHERON_AGENT_DIR": _resolve_default_agent_dir(cfg),
        "OPENHERON_BUILTIN_SKILLS_DIR": str(agent.get("builtinSkillsDir", "")).strip(),
        "OPENHERON_HEARTBEAT_EVERY": str(heartbeat.get("every", "30m")).strip() or "30m",
        "OPENHERON_HEARTBEAT_PROMPT": str(heartbeat.get("prompt", "")).strip(),
        "OPENHERON_HEARTBEAT_ACK_MAX_CHARS": str(
            _coerce_nonnegative_int(heartbeat.get("ackMaxChars", 300), default=300)
        ),
        "OPENHERON_HEARTBEAT_SHOW_OK": "1" if is_enabled(heartbeat.get("showOk"), default=False) else "0",
        "OPENHERON_HEARTBEAT_SHOW_ALERTS": "1"
        if is_enabled(heartbeat.get("showAlerts"), default=True)
        else "0",
        "OPENHERON_HEARTBEAT_TARGET": str(heartbeat.get("target", "last")).strip() or "last",
        "OPENHERON_HEARTBEAT_TARGET_CHANNEL": str(heartbeat.get("targetChannel", "")).strip(),
        "OPENHERON_HEARTBEAT_TARGET_CHAT_ID": str(heartbeat.get("targetChatId", "")).strip(),
        "OPENHERON_HEARTBEAT_ACTIVE_HOURS_START": str(active_hours.get("start", "")).strip(),
        "OPENHERON_HEARTBEAT_ACTIVE_HOURS_END": str(active_hours.get("end", "")).strip(),
        "OPENHERON_HEARTBEAT_ACTIVE_HOURS_TIMEZONE": str(active_hours.get("timezone", "user")).strip() or "user",
        "OPENHERON_SESSION_DB_URL": str(session.get("dbUrl", "")).strip(),
        "OPENHERON_CHANNELS": _resolve_enabled_channels(channels),
        "BRAVE_API_KEY": web_search_api_key,
        "OPENHERON_WEB_ENABLED": "1" if web_enabled else "0",
        "OPENHERON_WEB_SEARCH_ENABLED": "1" if web_search_enabled else "0",
        "OPENHERON_WEB_SEARCH_PROVIDER": web_search_provider,
        "OPENHERON_WEB_SEARCH_MAX_RESULTS": str(web_search_max_results),
        "OPENHERON_RESTRICT_TO_WORKSPACE": "1" if restrict_workspace else "0",
        "OPENHERON_ALLOW_EXEC": "1" if allow_exec else "0",
        "OPENHERON_ALLOW_NETWORK": "1" if allow_network else "0",
        "OPENHERON_EXEC_ALLOWLIST": exec_allowlist,
        "OPENHERON_MCP_SERVERS_JSON": mcp_servers_json,
        "OPENHERON_GUI_BUILTIN_TOOLS_ENABLED": "1"
        if is_enabled(gui.get("builtinToolsEnabled"), default=True)
        else "0",
        "OPENHERON_DEBUG": "1" if bool(debug) else "0",
    }
    env.update(gui_multimodal_env)
    env.update(channel_env)
    if provider_key_env:
        env[provider_key_env] = provider_api_key
    # Keep runtime env overrides as the final layer so they can override any mapped key.
    if runtime_env_overrides is None:
        env.update(_env_overrides(cfg))
    else:
        env.update(_env_overrides_from_mapping(runtime_env_overrides))
    return env


def _managed_env_keys() -> set[str]:
    """Keys controlled by config-to-env mapping."""
    return set(config_to_env(default_config()).keys())


def _active_provider_fallback_api_key_env(cfg: dict[str, Any]) -> str | None:
    """Return API-key env name to preserve when active provider key is omitted.

    When the selected provider is enabled but its config `apiKey` is empty, the
    runtime should keep an existing shell env API key instead of clearing it.
    """
    provider_name, provider_enabled, _, provider_api_key, _, _ = _resolve_provider(cfg)
    if not provider_enabled or provider_api_key:
        return None
    return provider_api_key_env(provider_name)


def apply_config_to_env(
    config: dict[str, Any],
    *,
    overwrite: bool = False,
    clear_missing: bool = False,
    runtime_env_overrides: dict[str, Any] | None = None,
) -> None:
    """Inject config fields into environment variables."""
    cfg = normalize_config(config)
    mapped = config_to_env(cfg, runtime_env_overrides=runtime_env_overrides)
    fallback_api_key_env = _active_provider_fallback_api_key_env(cfg)
    if clear_missing:
        for key in _managed_env_keys():
            if key not in mapped:
                os.environ.pop(key, None)

    for key, value in mapped.items():
        if not value and key != "OPENHERON_DEBUG":
            if clear_missing:
                if key == fallback_api_key_env and os.getenv(key, "").strip():
                    # Preserve shell key as fallback when config omits active key.
                    continue
                os.environ.pop(key, None)
            continue
        if overwrite or key not in os.environ:
            os.environ[key] = value


def bootstrap_env_from_config(config_path: Path | None = None) -> dict[str, Any] | None:
    """Load config file (if present) and apply values to process env."""
    path = config_path or get_config_path()
    if not path.exists():
        return None

    # Empty JSON object means "no config overrides" and should fall back to the
    # current shell environment.
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict) or not raw:
        return None

    cfg = normalize_config(raw)
    runtime_path = _runtime_config_path_for_config_path(path)
    runtime_overrides = _env_overrides(load_runtime_config(runtime_config_path=runtime_path))
    legacy_overrides = _env_overrides_from_mapping(raw.get("env"))
    default_overrides = _env_overrides(default_runtime_config())
    merged_runtime_overrides = {**default_overrides, **legacy_overrides, **runtime_overrides}
    # Config file is the source of truth for runtime bootstrap.
    apply_config_to_env(
        cfg,
        overwrite=True,
        clear_missing=True,
        runtime_env_overrides=merged_runtime_overrides,
    )
    return deepcopy(cfg)
