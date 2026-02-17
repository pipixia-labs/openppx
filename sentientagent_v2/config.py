"""Persistent config support for sentientagent_v2."""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any


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
            "model": "gemini-3-flash-preview",
            "workspace": str(get_default_workspace_path()),
            "builtinSkillsDir": "",
        },
        "session": {
            "backend": "memory",
            "dbUrl": "",
        },
        "channels": {
            "enabled": ["local"],
            "feishu": {
                "appId": "",
                "appSecret": "",
                "encryptKey": "",
                "verificationToken": "",
            },
        },
        "keys": {
            "braveApiKey": "",
        },
        "debug": False,
    }


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _deep_merge(base.get(key), value)
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


def _coerce_channels(value: Any) -> str:
    if isinstance(value, list):
        names = [str(item).strip().lower() for item in value if str(item).strip()]
        return ",".join(names) if names else "local"
    if isinstance(value, str):
        text = value.strip()
        return text or "local"
    return "local"


def config_to_env(config: dict[str, Any]) -> dict[str, str]:
    """Map config payload into runtime environment variables."""
    cfg = normalize_config(config)
    agent = cfg.get("agent", {})
    session = cfg.get("session", {})
    channels = cfg.get("channels", {})
    feishu = channels.get("feishu", {})
    keys = cfg.get("keys", {})
    debug = cfg.get("debug", False)

    env = {
        "SENTIENTAGENT_V2_MODEL": str(agent.get("model", "")).strip(),
        "SENTIENTAGENT_V2_WORKSPACE": str(agent.get("workspace", "")).strip(),
        "SENTIENTAGENT_V2_BUILTIN_SKILLS_DIR": str(agent.get("builtinSkillsDir", "")).strip(),
        "SENTIENTAGENT_V2_SESSION_BACKEND": str(session.get("backend", "")).strip().lower(),
        "SENTIENTAGENT_V2_SESSION_DB_URL": str(session.get("dbUrl", "")).strip(),
        "SENTIENTAGENT_V2_CHANNELS": _coerce_channels(channels.get("enabled")),
        "FEISHU_APP_ID": str(feishu.get("appId", "")).strip(),
        "FEISHU_APP_SECRET": str(feishu.get("appSecret", "")).strip(),
        "FEISHU_ENCRYPT_KEY": str(feishu.get("encryptKey", "")).strip(),
        "FEISHU_VERIFICATION_TOKEN": str(feishu.get("verificationToken", "")).strip(),
        "BRAVE_API_KEY": str(keys.get("braveApiKey", "")).strip(),
        "SENTIENTAGENT_V2_DEBUG": "1" if bool(debug) else "0",
    }
    return env


def apply_config_to_env(config: dict[str, Any], *, overwrite: bool = False) -> None:
    """Inject config fields into environment variables."""
    for key, value in config_to_env(config).items():
        if not value and key != "SENTIENTAGENT_V2_DEBUG":
            continue
        if overwrite or key not in os.environ:
            os.environ[key] = value


def bootstrap_env_from_config(config_path: Path | None = None) -> dict[str, Any] | None:
    """Load config file (if present) and apply values to process env."""
    path = config_path or get_config_path()
    if not path.exists():
        return None
    cfg = load_config(path)
    apply_config_to_env(cfg, overwrite=False)
    return deepcopy(cfg)
