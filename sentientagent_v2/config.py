"""Persistent config support for sentientagent_v2."""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from .env_utils import is_enabled

_DEFAULT_GOOGLE_MODEL = "gemini-3-flash-preview"


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
            "active": "google",
            "google": {
                "enabled": True,
                "apiKey": "",
                "model": _DEFAULT_GOOGLE_MODEL,
            },
            "openai": {
                "enabled": False,
                "apiKey": "",
                "model": "",
            },
            "openrouter": {
                "enabled": False,
                "apiKey": "",
                "model": "",
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
        "debug": False,
    }


def _deep_merge(base: Any, override: Any) -> Any:
    """Merge override into base, but keep only keys defined in base schema."""
    if isinstance(base, dict):
        if not isinstance(override, dict):
            return base
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
        print(f"Warning: failed to load config at {path}: {exc}", file=sys.stderr)
        return default_config()

    if not isinstance(data, dict):
        print(f"Warning: invalid config root at {path}; expected JSON object", file=sys.stderr)
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
    for name in ("local", "feishu"):
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

    active = str(providers.get("active", "google")).strip().lower() or "google"
    active_cfg = providers.get(active, {})
    if not isinstance(active_cfg, dict):
        active_cfg = {}

    enabled = is_enabled(active_cfg.get("enabled"), default=(active == "google"))
    model = str(active_cfg.get("model", "")).strip() or _DEFAULT_GOOGLE_MODEL
    api_key = str(active_cfg.get("apiKey", "")).strip()
    return active, enabled, model, api_key


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


def config_to_env(config: dict[str, Any]) -> dict[str, str]:
    """Map config payload into runtime environment variables."""
    cfg = normalize_config(config)
    agent = cfg.get("agent", {})
    session = cfg.get("session", {})
    channels = cfg.get("channels", {})
    feishu = channels.get("feishu", {}) if isinstance(channels, dict) else {}
    if not isinstance(feishu, dict):
        feishu = {}
    provider_name, provider_enabled, model, provider_api_key = _resolve_provider(cfg)
    web_enabled, web_search_enabled, web_search_provider, web_search_max_results, web_search_api_key = _resolve_web(
        cfg
    )
    debug = cfg.get("debug", False)

    env = {
        "GOOGLE_API_KEY": provider_api_key,
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
        "BRAVE_API_KEY": web_search_api_key,
        "SENTIENTAGENT_V2_WEB_ENABLED": "1" if web_enabled else "0",
        "SENTIENTAGENT_V2_WEB_SEARCH_ENABLED": "1" if web_search_enabled else "0",
        "SENTIENTAGENT_V2_WEB_SEARCH_PROVIDER": web_search_provider,
        "SENTIENTAGENT_V2_WEB_SEARCH_MAX_RESULTS": str(web_search_max_results),
        "SENTIENTAGENT_V2_DEBUG": "1" if bool(debug) else "0",
    }
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
