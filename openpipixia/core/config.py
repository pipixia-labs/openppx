"""Persistent config support for openpipixia."""

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
    canonical_provider_name,
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
    "dingtalk",
    "email",
    "slack",
    "qq",
    "weixin",
    "wecom",
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
    ("weixin", "baseUrl", "WEIXIN_BASE_URL"),
    ("weixin", "cdnBaseUrl", "WEIXIN_CDN_BASE_URL"),
    ("weixin", "token", "WEIXIN_TOKEN"),
    ("weixin", "stateDir", "WEIXIN_STATE_DIR"),
    ("wecom", "botId", "WECOM_BOT_ID"),
    ("wecom", "secret", "WECOM_SECRET"),
    ("wecom", "welcomeMessage", "WECOM_WELCOME_MESSAGE"),
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
    ("dingtalk", "allowFrom", "DINGTALK_ALLOW_FROM"),
    ("email", "allowFrom", "EMAIL_ALLOW_FROM"),
    ("slack", "allowFrom", "SLACK_ALLOW_FROM"),
    ("slack", "pollChannels", "SLACK_POLL_CHANNELS"),
    ("qq", "allowFrom", "QQ_ALLOW_FROM"),
    ("weixin", "allowFrom", "WEIXIN_ALLOW_FROM"),
    ("wecom", "allowFrom", "WECOM_ALLOW_FROM"),
)

# (channel_name, config_key, env_key, default)
_CHANNEL_FLAG_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("local", "streamingEnabled", "LOCAL_STREAMING_ENABLED", True),
    ("feishu", "streamingEnabled", "FEISHU_STREAMING_ENABLED", False),
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
    ("dingtalk", "streamReconnectDelaySeconds", "DINGTALK_STREAM_RECONNECT_DELAY_SECONDS", 5),
    ("email", "imapPort", "EMAIL_IMAP_PORT", 993),
    ("email", "smtpPort", "EMAIL_SMTP_PORT", 587),
    ("email", "pollIntervalSeconds", "EMAIL_POLL_INTERVAL_SECONDS", 30),
    ("email", "maxBodyChars", "EMAIL_MAX_BODY_CHARS", 12000),
    ("slack", "pollIntervalSeconds", "SLACK_POLL_INTERVAL_SECONDS", 15),
    ("weixin", "pollTimeoutSeconds", "WEIXIN_POLL_TIMEOUT_SECONDS", 35),
)

_EXTENSIBLE_MAP_KEYS: frozenset[str] = frozenset({"env", "multimodalProviders"})
_CONFIG_PATH_ENV = "OPENPPX_CONFIG_FILE"
_RUNTIME_CONFIG_PATH_ENV = "OPENPPX_RUNTIME_CONFIG_FILE"
_DATA_DIR_ENV = "OPENPPX_DATA_DIR"
_AGENT_HOME_ENV = "OPENPPX_AGENT_HOME"
_MEMORY_MARKDOWN_DIR_ENV = "OPENPPX_MEMORY_MARKDOWN_DIR"
_AGENT_PRIVILEGE_LEVEL_CANONICAL: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "root": "root",
}
_FILESYSTEM_ACCESS_VALUES: frozenset[str] = frozenset({"read_only", "read_write"})
_SHELL_DEBUG_ENV_KEYS: frozenset[str] = frozenset(
    {"OPENPPX_DEBUG", "OPENPPX_DEBUG_LOG_PATH"}
)


def get_data_dir() -> Path:
    """Return the data directory used by openpipixia."""
    explicit = os.getenv(_DATA_DIR_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".openppx"


def get_agent_home_dir() -> Path:
    """Return the active per-agent config directory.

    The agent home stores persistent metadata and support files such as
    ``AGENTS.md``, ``skills/``, and ``memory/``. When a specific config file is
    selected, its parent directory becomes the active agent home.
    """
    explicit = os.getenv(_AGENT_HOME_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()

    config_explicit = os.getenv(_CONFIG_PATH_ENV, "").strip()
    if config_explicit:
        return Path(config_explicit).expanduser().resolve(strict=False).parent

    return get_data_dir()


def get_config_path() -> Path:
    """Return the default config file path."""
    explicit = os.getenv(_CONFIG_PATH_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return get_data_dir() / "config.json"


def get_runtime_config_path() -> Path:
    """Return the default runtime config path for advanced env overrides."""
    explicit = os.getenv(_RUNTIME_CONFIG_PATH_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    config_explicit = os.getenv(_CONFIG_PATH_ENV, "").strip()
    if config_explicit:
        return Path(config_explicit).expanduser().with_name("runtime.json")
    return get_data_dir() / "runtime.json"


def get_default_workspace_path() -> Path:
    """Return default workspace path used by install initialization."""
    return get_data_dir() / "workspace"


def normalize_agent_privilege_level(value: Any, *, default: str = "low") -> str:
    """Normalize one privilege level name into a canonical Agent label."""
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    canonical = _AGENT_PRIVILEGE_LEVEL_CANONICAL.get(raw)
    if canonical:
        return canonical
    choices = ", ".join(_AGENT_PRIVILEGE_LEVEL_CANONICAL)
    raise ValueError(f"unsupported agent privilege level '{raw}'; expected one of: {choices}")


def privilege_level_default_permissions(privilege_level: str) -> dict[str, Any]:
    """Return the default permission profile for one canonical privilege level."""
    normalized = normalize_agent_privilege_level(privilege_level)
    if normalized in {"high", "root"}:
        return {
            "workspaceScope": "multi_workspace",
            "filesystemAccess": "read_write",
            "shellExec": "full",
            "networkAccess": "full",
            "toolAccess": "broad",
            "secretAccess": "limited",
            "canDelegate": True,
            "canApprovePrivilegeEscalation": True,
            "highRiskActionAccess": "conditional",
        }
    if normalized == "medium":
        return {
            "workspaceScope": "single_workspace",
            "filesystemAccess": "read_write",
            "shellExec": "restricted",
            "networkAccess": "restricted",
            "toolAccess": "task_scoped",
            "secretAccess": "limited",
            "canDelegate": True,
            "canApprovePrivilegeEscalation": False,
            "highRiskActionAccess": "false",
        }
    return {
        "workspaceScope": "single_workspace",
        "filesystemAccess": "read_only",
        "shellExec": "none",
        "networkAccess": "none",
        "toolAccess": "read_only",
        "secretAccess": "none",
        "canDelegate": False,
        "canApprovePrivilegeEscalation": False,
        "highRiskActionAccess": "false",
    }


def apply_agent_privilege_level_defaults(config: dict[str, Any], *, privilege_level: str) -> dict[str, Any]:
    """Apply one privilege-level profile to config and sync legacy security flags."""
    normalized_privilege_level = normalize_agent_privilege_level(privilege_level)
    permissions = privilege_level_default_permissions(normalized_privilege_level)

    agent = config.setdefault("agent", {})
    if not isinstance(agent, dict):
        agent = {}
        config["agent"] = agent
    agent["privilegeLevel"] = normalized_privilege_level
    agent["permissions"] = permissions

    security = config.setdefault("security", {})
    if not isinstance(security, dict):
        security = {}
        config["security"] = security

    if normalized_privilege_level in {"high", "root"}:
        security["restrictToWorkspace"] = False
        security["allowExec"] = True
        security["allowNetwork"] = True
        security["execAllowlist"] = []
    elif normalized_privilege_level == "medium":
        security["restrictToWorkspace"] = True
        security["allowExec"] = True
        security["allowNetwork"] = True
        security["execAllowlist"] = [
            "python",
            "python3",
            "bash",
            "sh",
            "zsh",
            "ls",
            "cat",
            "rg",
            "grep",
            "find",
            "sed",
            "awk",
            "head",
            "tail",
            "wc",
            "sort",
            "uniq",
            "cut",
            "tr",
            "tee",
            "touch",
            "mkdir",
            "cp",
            "mv",
            "pytest",
            "git",
        ]
    else:
        security["restrictToWorkspace"] = True
        security["allowExec"] = False
        security["allowNetwork"] = False
        security["execAllowlist"] = []
    return config


def _normalize_filesystem_access(value: Any) -> str:
    """Normalize filesystem access value into a supported policy token."""
    raw = str(value or "").strip().lower()
    if raw in _FILESYSTEM_ACCESS_VALUES:
        return raw
    return "read_write"


def _default_runtime_env_overrides() -> dict[str, Any]:
    """Return default runtime env overrides shown in generated config.

    These values cover runtime knobs that are primarily consumed from
    environment variables. Keeping them in config makes defaults explicit and
    easy to edit.
    """
    return {
        "OPENPPX_MEMORY_ENABLED": True,
        "OPENPPX_MEMORY_BACKEND": "markdown",
        # Keep empty by default so memory path follows the active agent home.
        _MEMORY_MARKDOWN_DIR_ENV: "",
        "OPENPPX_COMPACTION_ENABLED": True,
        "OPENPPX_COMPACTION_INTERVAL": 8,
        "OPENPPX_COMPACTION_OVERLAP": 1,
        "OPENPPX_COMPACTION_TOKEN_THRESHOLD": "",
        "OPENPPX_COMPACTION_EVENT_RETENTION": "",
        "OPENPPX_BOOTSTRAP_MAX_CHARS_PER_FILE": 12000,
        "OPENPPX_BOOTSTRAP_MAX_TOTAL_CHARS": 30000,
        "OPENPPX_SUBAGENT_MAX_CONCURRENCY": 2,
        "OPENPPX_MCP_REQUIRED_SERVERS": "",
        "OPENPPX_MCP_PROBE_RETRY_ATTEMPTS": 2,
        "OPENPPX_MCP_PROBE_RETRY_BACKOFF_SECONDS": 0.3,
        "OPENPPX_MCP_DOCTOR_TIMEOUT_SECONDS": 5,
        "OPENPPX_MCP_GATEWAY_TIMEOUT_SECONDS": 5,
        "OPENPPX_WHATSAPP_BRIDGE_PRECHECK": True,
        "OPENPPX_WHATSAPP_BRIDGE_SOURCE": "",
        "OPENPPX_DEBUG_MAX_CHARS": 0,
    }


def _normalize_runtime_memory_dir_override(
    runtime_overrides: dict[str, str],
    *,
    config_path: Path,
) -> dict[str, str]:
    """Rewrite legacy global memory dir override to per-agent memory dir.

    Historical runtime defaults wrote `OPENPPX_MEMORY_MARKDOWN_DIR` as the
    global `~/.openpipixia/workspace/memory`, which breaks per-agent isolation.
    During bootstrap we remap only this exact legacy value to the current
    agent-scoped default (`<agent_data_dir>/memory`).
    """
    value = str(runtime_overrides.get(_MEMORY_MARKDOWN_DIR_ENV, "")).strip()
    if not value:
        return runtime_overrides

    legacy_global_default = (Path.home() / ".openpipixia" / "workspace" / "memory").resolve(strict=False)
    try:
        current = Path(value).expanduser().resolve(strict=False)
    except Exception:
        return runtime_overrides
    if current != legacy_global_default:
        return runtime_overrides

    scoped_default = (config_path.parent / "memory").resolve(strict=False)
    if scoped_default == legacy_global_default:
        return runtime_overrides

    patched = dict(runtime_overrides)
    patched[_MEMORY_MARKDOWN_DIR_ENV] = str(scoped_default)
    return patched


def default_config() -> dict[str, Any]:
    """Build default config content."""
    return {
        "agent": {
            "name": "",
            "privilegeLevel": "",
            "permissions": {},
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
        "multimodalProviders": {
            "openai_mm": {
                "enabled": False,
                "provider": "openai",
                "apiKey": "",
                "model": "gpt-5.4",
                "apiBase": "",
                "extraHeaders": {},
            },
            "google_mm": {
                "enabled": False,
                "provider": "google",
                "apiKey": "",
                "model": "gemini-3-flash-preview",
                "apiBase": "",
                "extraHeaders": {},
            },
            "qwen_mm": {
                "enabled": False,
                "provider": "qwen",
                "apiKey": "",
                "model": "",
                "apiBase": "",
                "extraHeaders": {},
            },
        },
        "gui": {
            "groundingProvider": "openai_mm",
            "plannerProvider": "openai_mm",
            "builtinGUIToolsEnabled": True,
        },
        "session": {
            "dbUrl": "",
        },
        "channels": {
            "local": {
                "enabled": True,
                "streamingEnabled": True,
            },
            "feishu": {
                "enabled": False,
                "appId": "",
                "appSecret": "",
                "encryptKey": "",
                "verificationToken": "",
                "allowFrom": [],
                "streamingEnabled": False,
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
            "weixin": {
                "enabled": False,
                "baseUrl": "https://ilinkai.weixin.qq.com",
                "cdnBaseUrl": "https://novac2c.cdn.weixin.qq.com/c2c",
                "token": "",
                "stateDir": "",
                "pollTimeoutSeconds": 35,
                "allowFrom": [],
            },
            "wecom": {
                "enabled": False,
                "botId": "",
                "secret": "",
                "allowFrom": [],
                "welcomeMessage": "",
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
    raw_agent = raw.get("agent") if isinstance(raw, dict) else None
    if isinstance(raw_agent, dict) and "role" in raw_agent:
        raise ValueError(
            "agent.role has been removed; use agent.privilegeLevel with one of: low, medium, high, root"
        )
    cfg = _deep_merge(default_config(), raw or {})
    if not isinstance(cfg, dict):
        return default_config()
    agent = _as_dict(cfg.get("agent"))
    agent["privilegeLevel"] = normalize_agent_privilege_level(agent.get("privilegeLevel", ""), default="")
    cfg["agent"] = agent
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
    try:
        return normalize_config(data)
    except ValueError as exc:
        raise ValueError(f"invalid config at {path}: {exc}") from exc


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


def _activate_config_context(config_path: Path) -> None:
    """Bind process-level config/runtime/data root for one selected config file."""
    resolved = config_path.expanduser().resolve(strict=False)
    runtime_path = _runtime_config_path_for_config_path(resolved).resolve(strict=False)
    data_dir = resolved.parent.resolve(strict=False)
    os.environ[_CONFIG_PATH_ENV] = str(resolved)
    os.environ[_RUNTIME_CONFIG_PATH_ENV] = str(runtime_path)
    os.environ[_DATA_DIR_ENV] = str(data_dir)
    os.environ[_AGENT_HOME_ENV] = str(data_dir)


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


def _resolve_security(cfg: dict[str, Any]) -> tuple[bool, bool, bool, str, str]:
    security = cfg.get("security")
    if not isinstance(security, dict):
        security = {}
    agent = cfg.get("agent")
    if not isinstance(agent, dict):
        agent = {}
    permissions = agent.get("permissions")
    filesystem_access = "read_write"
    if isinstance(permissions, dict) and permissions:
        filesystem_access = _normalize_filesystem_access(permissions.get("filesystemAccess"))

    restrict = is_enabled(security.get("restrictToWorkspace"), default=False)
    allow_exec = is_enabled(security.get("allowExec"), default=True)
    allow_network = is_enabled(security.get("allowNetwork"), default=True)

    raw_allowlist = security.get("execAllowlist", [])
    if not isinstance(raw_allowlist, list):
        raw_allowlist = []
    allowlist = ",".join(normalize_allowlist(raw_allowlist))
    return restrict, allow_exec, allow_network, allowlist, filesystem_access


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


def _resolve_gui_provider_env(cfg: dict[str, Any], *, provider_name: str) -> tuple[str, str, str, str]:
    """Resolve one GUI multimodal provider alias into model/api settings and provider identity."""
    name = str(provider_name).strip()
    if not name:
        return "", "", "", ""

    providers = cfg.get("multimodalProviders")
    if not isinstance(providers, dict):
        return "", "", "", canonical_provider_name(name)
    raw = providers.get(name, {})
    if not isinstance(raw, dict):
        return "", "", "", canonical_provider_name(name)
    resolved_provider = canonical_provider_name(str(raw.get("provider", "")).strip() or name)
    if not is_enabled(raw.get("enabled"), default=False):
        return "", "", "", resolved_provider

    model = str(raw.get("model", "")).strip()
    api_key = str(raw.get("apiKey", "")).strip()
    if not api_key:
        api_key_env = provider_api_key_env(resolved_provider)
        if api_key_env:
            api_key = os.getenv(api_key_env, "").strip()
    api_base = str(raw.get("apiBase", "")).strip()
    return model, api_key, api_base, resolved_provider


def _resolve_gui_multimodal_env(cfg: dict[str, Any]) -> dict[str, str]:
    """Resolve GUI grounding/planner env values from multimodalProviders config."""
    gui = cfg.get("gui")
    if not isinstance(gui, dict):
        gui = {}

    grounding_model, _, grounding_api_base, grounding_provider = _resolve_gui_provider_env(
        cfg,
        provider_name=str(gui.get("groundingProvider", "")),
    )
    planner_model, _, planner_api_base, planner_provider = _resolve_gui_provider_env(
        cfg,
        provider_name=str(gui.get("plannerProvider", "")),
    )

    return {
        "OPENPPX_GUI_MODEL": grounding_model,
        "OPENPPX_GUI_BASE_URL": grounding_api_base,
        "OPENPPX_GUI_PLANNER_MODEL": planner_model,
        "OPENPPX_GUI_PLANNER_BASE_URL": planner_api_base,
        "OPENPPX_GUI_GROUNDING_PROVIDER": grounding_provider,
        "OPENPPX_GUI_PLANNER_PROVIDER": planner_provider,
    }


def _resolve_gui_provider_api_key_env(cfg: dict[str, Any]) -> dict[str, str]:
    """Resolve provider-bound API key env overrides needed by GUI runtime."""
    gui = cfg.get("gui")
    if not isinstance(gui, dict):
        gui = {}

    api_env: dict[str, str] = {}

    for gui_key in ("groundingProvider", "plannerProvider"):
        provider_name = str(gui.get(gui_key, "")).strip()
        if not provider_name:
            continue
        _, api_key, _, resolved_provider = _resolve_gui_provider_env(cfg, provider_name=provider_name)
        env_name = provider_api_key_env(resolved_provider)
        if env_name and api_key:
            api_env[env_name] = api_key

    return api_env


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
    restrict_workspace, allow_exec, allow_network, exec_allowlist, filesystem_access = _resolve_security(cfg)
    mcp_servers_json = _resolve_mcp_servers_json(cfg)
    gui_multimodal_env = _resolve_gui_multimodal_env(cfg)
    gui_provider_api_env = _resolve_gui_provider_api_key_env(cfg)
    debug = cfg.get("debug", False)

    provider_key_env = provider_api_key_env(provider_name) if provider_enabled else None
    env = {
        **{env_key: "" for env_key in provider_api_key_env_keys()},
        "OPENPPX_MODEL": model,
        "OPENPPX_PROVIDER": provider_name,
        "OPENPPX_PROVIDER_ENABLED": "1" if provider_enabled else "0",
        "OPENPPX_PROVIDER_API_BASE": provider_api_base,
        "OPENPPX_PROVIDER_EXTRA_HEADERS_JSON": provider_extra_headers,
        "OPENPPX_AGENT_NAME": str(agent.get("name", "")).strip(),
        "OPENPPX_AGENT_HOME": os.getenv(_AGENT_HOME_ENV, "").strip(),
        "OPENPPX_AGENT_PRIVILEGE_LEVEL": normalize_agent_privilege_level(
            agent.get("privilegeLevel", ""),
            default="",
        ),
        "OPENPPX_CAN_DELEGATE": "1"
        if bool(_as_dict(agent.get("permissions")).get("canDelegate", False))
        else "0",
        "OPENPPX_CAN_APPROVE_PRIVILEGE_ESCALATION": "1"
        if bool(_as_dict(agent.get("permissions")).get("canApprovePrivilegeEscalation", False))
        else "0",
        "OPENPPX_HIGH_RISK_ACTION_ACCESS": str(
            _as_dict(agent.get("permissions")).get("highRiskActionAccess", "false")
        ).strip()
        or "false",
        "OPENPPX_WORKSPACE": str(agent.get("workspace", "")).strip(),
        "OPENPPX_BUILTIN_SKILLS_DIR": str(agent.get("builtinSkillsDir", "")).strip(),
        "OPENPPX_HEARTBEAT_EVERY": str(heartbeat.get("every", "30m")).strip() or "30m",
        "OPENPPX_HEARTBEAT_PROMPT": str(heartbeat.get("prompt", "")).strip(),
        "OPENPPX_HEARTBEAT_ACK_MAX_CHARS": str(
            _coerce_nonnegative_int(heartbeat.get("ackMaxChars", 300), default=300)
        ),
        "OPENPPX_HEARTBEAT_SHOW_OK": "1" if is_enabled(heartbeat.get("showOk"), default=False) else "0",
        "OPENPPX_HEARTBEAT_SHOW_ALERTS": "1"
        if is_enabled(heartbeat.get("showAlerts"), default=True)
        else "0",
        "OPENPPX_HEARTBEAT_TARGET": str(heartbeat.get("target", "last")).strip() or "last",
        "OPENPPX_HEARTBEAT_TARGET_CHANNEL": str(heartbeat.get("targetChannel", "")).strip(),
        "OPENPPX_HEARTBEAT_TARGET_CHAT_ID": str(heartbeat.get("targetChatId", "")).strip(),
        "OPENPPX_HEARTBEAT_ACTIVE_HOURS_START": str(active_hours.get("start", "")).strip(),
        "OPENPPX_HEARTBEAT_ACTIVE_HOURS_END": str(active_hours.get("end", "")).strip(),
        "OPENPPX_HEARTBEAT_ACTIVE_HOURS_TIMEZONE": str(active_hours.get("timezone", "user")).strip() or "user",
        "OPENPPX_SESSION_DB_URL": str(session.get("dbUrl", "")).strip(),
        "OPENPPX_CHANNELS": _resolve_enabled_channels(channels),
        "BRAVE_API_KEY": web_search_api_key,
        "OPENPPX_WEB_ENABLED": "1" if web_enabled else "0",
        "OPENPPX_WEB_SEARCH_ENABLED": "1" if web_search_enabled else "0",
        "OPENPPX_WEB_SEARCH_PROVIDER": web_search_provider,
        "OPENPPX_WEB_SEARCH_MAX_RESULTS": str(web_search_max_results),
        "OPENPPX_RESTRICT_TO_WORKSPACE": "1" if restrict_workspace else "0",
        "OPENPPX_FILESYSTEM_ACCESS": filesystem_access,
        "OPENPPX_ALLOW_EXEC": "1" if allow_exec else "0",
        "OPENPPX_ALLOW_NETWORK": "1" if allow_network else "0",
        "OPENPPX_EXEC_ALLOWLIST": exec_allowlist,
        "OPENPPX_MCP_SERVERS_JSON": mcp_servers_json,
        "OPENPPX_GUI_BUILTIN_TOOLS_ENABLED": "1"
        if is_enabled(gui.get("builtinGUIToolsEnabled"), default=True)
        else "0",
        "OPENPPX_DEBUG": "1" if bool(debug) else "0",
    }
    env.update(gui_multimodal_env)
    env.update(gui_provider_api_env)
    env.update(channel_env)
    if provider_key_env and provider_api_key:
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
    shell_debug_overrides = {
        key: os.environ[key]
        for key in _SHELL_DEBUG_ENV_KEYS
        if os.getenv(key, "").strip()
    }
    if clear_missing:
        for key in _managed_env_keys():
            if key not in mapped:
                os.environ.pop(key, None)

    for key, value in mapped.items():
        if not value and key != "OPENPPX_DEBUG":
            if clear_missing:
                if key == fallback_api_key_env and os.getenv(key, "").strip():
                    # Preserve shell key as fallback when config omits active key.
                    continue
                os.environ.pop(key, None)
            continue
        if overwrite or key not in os.environ:
            os.environ[key] = value

    # Keep explicit shell-provided debug flags for temporary diagnostics.
    for key, value in shell_debug_overrides.items():
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

    _activate_config_context(path)
    try:
        cfg = normalize_config(raw)
    except ValueError as exc:
        raise ValueError(f"invalid config at {path}: {exc}") from exc
    runtime_path = _runtime_config_path_for_config_path(path)
    runtime_overrides = _env_overrides(load_runtime_config(runtime_config_path=runtime_path))
    legacy_overrides = _env_overrides_from_mapping(raw.get("env"))
    default_overrides = _env_overrides(default_runtime_config())
    merged_runtime_overrides = {**default_overrides, **legacy_overrides, **runtime_overrides}
    merged_runtime_overrides = _normalize_runtime_memory_dir_override(
        merged_runtime_overrides,
        config_path=path,
    )
    # Config file is the source of truth for runtime bootstrap.
    apply_config_to_env(
        cfg,
        overwrite=True,
        clear_missing=True,
        runtime_env_overrides=merged_runtime_overrides,
    )
    return deepcopy(cfg)
