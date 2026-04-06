"""Minimal CLI helpers for openpipixia."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib.util
import json
import os
import signal
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlparse

from loguru import logger

from ..core.config import (
    apply_agent_role_defaults,
    bootstrap_env_from_config,
    default_config,
    default_runtime_config,
    get_data_dir,
    get_config_path,
    get_runtime_config_path,
    load_config,
    load_runtime_config,
    normalize_agent_role,
    save_config,
    save_runtime_config,
)
from ..core.env_utils import env_enabled, is_enabled
from ..core import doctor_rules
from ..core.logging_utils import debug_logging_enabled, emit_debug
from ..core.gui_mcp import resolve_gui_mcp_from_env, resolve_gui_mcp_from_summaries
from ..core.provider import (
    DEFAULT_PROVIDER,
    canonical_provider_name,
    normalize_model_name,
    normalize_provider_name,
    oauth_provider_names,
    provider_api_key_env,
    provider_names,
    validate_provider_runtime,
)
from ..core.provider_registry import find_provider_spec
from ..runtime.cron_helpers import cron_store_path, format_schedule, format_timestamp_ms
from ..runtime.cron_service import CronService
from ..runtime.heartbeat_status_store import read_heartbeat_status_snapshot
from ..runtime.token_usage_store import read_token_usage_stats
from ..runtime.gateway_service import (
    detect_service_manager,
    gateway_service_name,
    render_launchd_plist,
    render_systemd_unit,
)
from ..runtime.message_time import inject_request_time
from ..core.security import load_security_policy
from ..tooling.skills_adapter import get_registry
from . import cli_runtime_surface
from . import cli_runtime_ops
from . import cli_gateway_surface


def _stdout_line(message: str) -> None:
    """Write one plain user-facing line to stdout (without Loguru formatting)."""
    print(message)


def _doctor_debug_log_path() -> Path:
    """Return scoped doctor debug log path under current data directory."""
    log_dir = get_data_dir() / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "doctor.debug.log"


def _append_doctor_debug_lines(lines: list[str]) -> Path | None:
    """Append doctor debug lines into scoped doctor debug log file."""
    if not lines:
        return None
    try:
        path = _doctor_debug_log_path()
        ts = dt.datetime.now().astimezone().isoformat()
        payload = [f"[{ts}] [doctor] {line}" for line in lines]
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(payload) + "\n")
        return path
    except Exception:
        # Doctor diagnostics should not fail because debug log writing is unavailable.
        return None


def _parse_csv_list(raw: str) -> list[str]:
    """Parse comma-separated names, preserving order and removing duplicates."""
    seen: set[str] = set()
    names: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _doctor_parse_enabled_channels() -> list[str]:
    """Parse enabled channels for doctor checks without importing channel factory."""
    raw = os.getenv("OPENPPX_CHANNELS", "local")
    names = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if not names:
        return ["local"]
    return list(dict.fromkeys(names))


def _doctor_session_db_url() -> str:
    """Resolve doctor session DB URL without importing runtime session service stack."""
    value = os.getenv("OPENPPX_SESSION_DB_URL", "").strip()
    if value:
        return value
    return load_session_config().db_url


def _parse_enabled_channels(raw: str | None) -> list[str]:
    from ..channels.factory import parse_enabled_channels

    return parse_enabled_channels(raw)


def _validate_channel_setup(names: list[str]) -> list[str]:
    from ..channels.factory import validate_channel_setup

    return validate_channel_setup(names)


def _build_channel_manager(*, bus: Any, channel_names: list[str], local_writer: Callable[[str], None]) -> tuple[Any, Any]:
    from ..channels.factory import build_channel_manager

    return build_channel_manager(bus=bus, channel_names=channel_names, local_writer=local_writer)


# Backward-compatible patch points for tests and integrations.
parse_enabled_channels = _parse_enabled_channels
validate_channel_setup = _validate_channel_setup
build_channel_manager = _build_channel_manager


def _load_mcp_registry_symbols() -> tuple[Any, Any, Any, Any]:
    """Load MCP registry symbols lazily to keep lightweight commands fast."""
    from ..core import mcp_registry as _mcp_registry

    return (
        _mcp_registry.ManagedMcpToolset,
        _mcp_registry.build_mcp_toolsets_from_env,
        _mcp_registry.probe_mcp_toolsets,
        _mcp_registry.summarize_mcp_toolsets,
    )


class ManagedMcpToolset:  # Compatibility proxy for tests patching cli.ManagedMcpToolset.get_tools
    @staticmethod
    async def get_tools(toolset: Any) -> Any:
        real_cls, *_ = _load_mcp_registry_symbols()
        return await real_cls.get_tools(toolset)


def build_mcp_toolsets_from_env(*args: Any, **kwargs: Any) -> list[Any]:
    _, build_fn, _, _ = _load_mcp_registry_symbols()
    return build_fn(*args, **kwargs)


async def probe_mcp_toolsets(*args: Any, **kwargs: Any) -> Any:
    _, _, probe_fn, _ = _load_mcp_registry_symbols()
    return await probe_fn(*args, **kwargs)


def summarize_mcp_toolsets(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    _, _, _, summarize_fn = _load_mcp_registry_symbols()
    return summarize_fn(*args, **kwargs)


def _load_runtime_adk_utils_symbols() -> tuple[Any, Any]:
    """Load ADK text helpers lazily to avoid session stack import at startup."""
    from ..runtime import adk_utils as _adk_utils

    return _adk_utils.extract_text, _adk_utils.merge_text_stream


def extract_text(*args: Any, **kwargs: Any) -> Any:
    extract_text_fn, _ = _load_runtime_adk_utils_symbols()
    return extract_text_fn(*args, **kwargs)


def merge_text_stream(*args: Any, **kwargs: Any) -> Any:
    _, merge_text_stream_fn = _load_runtime_adk_utils_symbols()
    return merge_text_stream_fn(*args, **kwargs)


def _load_runner_factory_symbol() -> Any:
    from ..runtime import runner_factory as _runner_factory

    return _runner_factory.create_runner


def create_runner(*args: Any, **kwargs: Any) -> Any:
    return _load_runner_factory_symbol()(*args, **kwargs)


def _load_session_service_symbol() -> Any:
    from ..runtime import session_service as _session_service

    return _session_service.load_session_config


def load_session_config(*args: Any, **kwargs: Any) -> Any:
    return _load_session_service_symbol()(*args, **kwargs)


def _read_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Read an int env var with clamped bounds."""
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except Exception:
        value = default
    return min(max(value, minimum), maximum)


def _read_env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    """Read a float env var with clamped bounds."""
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except Exception:
        value = default
    return min(max(value, minimum), maximum)


@dataclass(frozen=True)
class McpProbePolicy:
    """Policy knobs for MCP health checks."""

    timeout_seconds: float
    retry_attempts: int
    retry_backoff_seconds: float


LEGACY_PROVIDER_FIELD_MIGRATIONS = doctor_rules.LEGACY_PROVIDER_FIELD_MIGRATIONS
LEGACY_CHANNEL_FIELD_MIGRATIONS = doctor_rules.LEGACY_CHANNEL_FIELD_MIGRATIONS
CHANNEL_ENV_BACKFILL_MAPPINGS = doctor_rules.CHANNEL_ENV_BACKFILL_MAPPINGS


DoctorFixOutcome = Literal["applied", "skipped", "failed"]


DOCTOR_CHANNEL_ENV_BACKFILL_RULES = doctor_rules.DOCTOR_CHANNEL_ENV_BACKFILL_RULES


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


DOCTOR_CHANNEL_BOOL_ENV_BACKFILL_RULES = doctor_rules.DOCTOR_CHANNEL_BOOL_ENV_BACKFILL_RULES


def _doctor_record_event(
    *,
    event_sink: list[dict[str, str]] | None,
    outcome: DoctorFixOutcome,
    code: str,
    rule: str,
    message: str,
) -> None:
    """Record a structured doctor-fix event for optional downstream diagnostics."""
    if event_sink is None:
        return
    event_sink.append(
        {
            "outcome": outcome,
            "code": code,
            "rule": rule,
            "message": message,
        }
    )


def _doctor_add_change(
    changes: list[str],
    *,
    event_sink: list[dict[str, str]] | None,
    code: str,
    rule: str,
    message: str,
) -> None:
    """Append one applied change and its structured event."""
    changes.append(message)
    _doctor_record_event(event_sink=event_sink, outcome="applied", code=code, rule=rule, message=message)


def _doctor_add_skipped(
    skipped: list[str],
    *,
    event_sink: list[dict[str, str]] | None,
    code: str,
    rule: str,
    message: str,
) -> None:
    """Append one skipped item and its structured event."""
    skipped.append(message)
    _doctor_record_event(event_sink=event_sink, outcome="skipped", code=code, rule=rule, message=message)


def _doctor_add_failed(
    failed: list[str],
    *,
    event_sink: list[dict[str, str]] | None,
    code: str,
    rule: str,
    message: str,
) -> None:
    """Append one failed item and its structured event."""
    failed.append(message)
    _doctor_record_event(event_sink=event_sink, outcome="failed", code=code, rule=rule, message=message)


def _load_mcp_probe_policy(*, timeout_env_name: str, timeout_default: float) -> McpProbePolicy:
    """Load MCP probe policy from env with one consistent rule set."""
    return McpProbePolicy(
        timeout_seconds=_read_env_float(
            timeout_env_name,
            timeout_default,
            minimum=1.0,
            maximum=30.0,
        ),
        retry_attempts=_read_env_int(
            "OPENPPX_MCP_PROBE_RETRY_ATTEMPTS",
            2,
            minimum=1,
            maximum=5,
        ),
        retry_backoff_seconds=_read_env_float(
            "OPENPPX_MCP_PROBE_RETRY_BACKOFF_SECONDS",
            0.3,
            minimum=0.0,
            maximum=5.0,
        ),
    )


def _cmd_skills(*, agent: str | None = None) -> int:
    return cli_runtime_surface.cmd_skills(
        agent=agent,
        stdout_line=_stdout_line,
        resolve_target_agent_names=lambda value: _resolve_target_agent_names(agent=value),
        run_agent_cli_command=lambda name, args: _run_agent_cli_command(agent_name=name, args=args),
    )


async def _collect_connected_mcp_apis(
    toolsets: list[Any],
    *,
    timeout_seconds: float,
) -> dict[str, list[dict[str, str]]]:
    return await cli_runtime_surface.collect_connected_mcp_apis(
        toolsets,
        timeout_seconds=timeout_seconds,
        get_tools_fn=ManagedMcpToolset.get_tools,
    )


def _cmd_mcps(*, agent: str | None = None) -> int:
    return cli_runtime_surface.cmd_mcps(
        agent=agent,
        stdout_line=_stdout_line,
        resolve_target_agent_names=lambda value: _resolve_target_agent_names(agent=value),
        run_agent_cli_command=lambda name, args: _run_agent_cli_command(agent_name=name, args=args),
        print_agent_output_sections=_print_agent_output_sections,
        load_mcp_probe_policy=_load_mcp_probe_policy,
        build_mcp_toolsets_from_env_fn=build_mcp_toolsets_from_env,
        probe_mcp_toolsets_fn=probe_mcp_toolsets,
        collect_connected_mcp_apis_fn=_collect_connected_mcp_apis,
    )


def _subagent_log_path() -> Path:
    """Return JSONL log path that records accepted `spawn_subagent` tasks."""
    return load_security_policy().workspace_root / ".openppx" / "subagents.log"


def _read_subagent_records(*, limit: int = 50) -> list[dict[str, Any]]:
    """Read sub-agent spawn records from local JSONL log (newest first)."""
    log_path = _subagent_log_path()
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").splitlines()
    records: list[dict[str, Any]] = []
    for raw_line in reversed(lines):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except Exception:
            continue
        if isinstance(payload, dict):
            records.append(payload)
        if len(records) >= max(1, int(limit)):
            break
    return records


def _cmd_spawn(*, agent: str | None = None) -> int:
    return cli_runtime_surface.cmd_spawn(
        agent=agent,
        stdout_line=_stdout_line,
        resolve_target_agent_names=lambda value: _resolve_target_agent_names(agent=value),
        run_agent_cli_command=lambda name, args: _run_agent_cli_command(agent_name=name, args=args),
        print_agent_output_sections=_print_agent_output_sections,
        read_subagent_records=lambda limit: _read_subagent_records(limit=limit),
    )


def _cmd_list(*, output_json: bool) -> int:
    """List known agents with role, workspace, and enablement state."""
    entries = _load_global_config_entries()
    enabled_names = {entry["name"] for entry in entries if bool(entry.get("enabled"))}
    data_dir = get_data_dir()
    discovered_names: list[str] = []
    if data_dir.exists():
        for child in sorted(data_dir.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir() or child.name.startswith(".") or child.name == "skills":
                continue
            if (child / "config.json").exists():
                discovered_names.append(child.name)

    ordered_names: list[str] = []
    seen: set[str] = set()
    for name in [*discovered_names, *[entry["name"] for entry in entries]]:
        if name in seen:
            continue
        seen.add(name)
        ordered_names.append(name)

    payload: list[dict[str, Any]] = []
    for agent_name in ordered_names:
        config_path = _agent_config_path(agent_name)
        role = ""
        workspace = ""
        if config_path.exists():
            cfg = load_config(config_path=config_path)
            role = str(cfg.get("agent", {}).get("role", "")).strip()
            workspace = str(cfg.get("agent", {}).get("workspace", "")).strip()
        payload.append(
            {
                "name": agent_name,
                "enabled": agent_name in enabled_names,
                "role": role,
                "workspace": workspace,
                "configPath": str(config_path),
            }
        )

    if output_json:
        _stdout_line(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not payload:
        _stdout_line("No agents found.")
        return 0

    _stdout_line("Agents:")
    for item in payload:
        enabled_label = "enabled" if item["enabled"] else "disabled"
        role_label = item["role"] or "unknown"
        workspace_label = item["workspace"] or "(unset)"
        _stdout_line(f"- {item['name']} [{enabled_label}] role={role_label} workspace={workspace_label}")
    return 0


def _cmd_enable_disable(*, name: str, enabled: bool) -> int:
    """Enable or disable one known agent in global_config.json."""
    normalized_name = _normalize_agent_name(name)
    if not normalized_name:
        _stdout_line("Error: agent name is empty.")
        return 2

    config_path = _agent_config_path(normalized_name)
    if not config_path.exists():
        _stdout_line(f"Error: agent '{normalized_name}' config not found: {config_path}")
        return 1

    global_config_path, existed = _set_global_agent_enabled(agent_name=normalized_name, enabled=enabled)
    action = "Enabled" if enabled else "Disabled"
    _stdout_line(f"{action} agent: {normalized_name}")
    _stdout_line(f"Global config updated: {global_config_path}")
    if not existed:
        _stdout_line(f"Agent '{normalized_name}' was added to global_config.json.")
    return 0


def _cmd_delete(*, name: str) -> int:
    """Delete one agent config directory and remove it from global_config.json."""
    normalized_name = _normalize_agent_name(name)
    if not normalized_name:
        _stdout_line("Error: agent name is empty.")
        return 2

    agent_dir = get_data_dir() / normalized_name
    config_path = agent_dir / "config.json"
    if not config_path.exists():
        _stdout_line(f"Error: agent '{normalized_name}' config not found: {config_path}")
        return 1

    shutil.rmtree(agent_dir)
    global_config_path = _remove_global_agent_entry(normalized_name)
    _stdout_line(f"Deleted agent: {normalized_name}")
    _stdout_line(f"Removed directory: {agent_dir}")
    _stdout_line(f"Global config updated: {global_config_path}")
    return 0


def _check_openai_codex_oauth() -> tuple[bool, str]:
    """Check whether OpenAI Codex OAuth token is locally available."""
    try:
        from oauth_cli_kit import get_token
    except ImportError:
        return False, "oauth-cli-kit is not installed. Run: pip install oauth-cli-kit"

    try:
        token = get_token()
    except Exception as exc:
        return False, f"failed to read oauth token store ({exc})"

    if not token or not getattr(token, "access", ""):
        return False, "token missing"
    if not getattr(token, "account_id", ""):
        return False, "account_id missing in token"
    return True, f"account_id={token.account_id}"


def _check_github_copilot_oauth_non_invasive() -> tuple[bool, str]:
    """Check GitHub Copilot OAuth cache files without triggering network/device flow.

    Read-only policy:
    - access token file: ~/.config/litellm/github_copilot/access-token
    - api key cache file: ~/.config/litellm/github_copilot/api-key.json
    """
    token_dir = Path(
        os.getenv(
            "GITHUB_COPILOT_TOKEN_DIR",
            str(Path.home() / ".config/litellm/github_copilot"),
        )
    )
    access_path = token_dir / os.getenv("GITHUB_COPILOT_ACCESS_TOKEN_FILE", "access-token")
    api_key_path = token_dir / os.getenv("GITHUB_COPILOT_API_KEY_FILE", "api-key.json")

    access_token = ""
    if access_path.exists():
        try:
            access_token = access_path.read_text(encoding="utf-8").strip()
        except Exception as exc:
            return False, f"failed to read access token cache ({exc})"

    api_key_payload: dict[str, Any] = {}
    if api_key_path.exists():
        try:
            raw = json.loads(api_key_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                api_key_payload = raw
        except Exception as exc:
            return False, f"failed to parse api-key cache ({exc})"

    api_key_token = str(api_key_payload.get("token", "")).strip()
    expires_at = api_key_payload.get("expires_at")
    expires_ts: float | None = None
    if expires_at is not None:
        try:
            expires_ts = float(expires_at)
        except Exception:
            return False, "invalid expires_at in api-key cache"

    now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    if api_key_token and expires_ts and expires_ts > now_ts:
        expiry_iso = dt.datetime.fromtimestamp(expires_ts, dt.timezone.utc).isoformat()
        return True, f"api_key_cached_until={expiry_iso}"

    if access_token:
        if api_key_token and expires_ts and expires_ts <= now_ts:
            return True, "access_token_cached_api_key_expired_refreshable"
        return True, "access_token_cached"

    return False, "access_token_missing"


def _provider_oauth_health(provider_name: str) -> tuple[str | None, dict[str, Any]]:
    """Return optional oauth issue and provider oauth health summary."""
    spec = find_provider_spec(provider_name)
    status: dict[str, Any] = {
        "required": bool(spec and spec.is_oauth),
        "authenticated": None,
        "message": "",
    }
    if not spec or not spec.is_oauth:
        status["authenticated"] = True
        status["message"] = "not_required"
        return None, status

    if spec.name == "openai_codex":
        ok, detail = _check_openai_codex_oauth()
        status["authenticated"] = ok
        status["message"] = detail
        if ok:
            return None, status
        return (
            "OpenAI Codex OAuth token is not ready "
            f"({detail}). Run: ppx provider login openai-codex",
            status,
        )

    if spec.name == "github_copilot":
        ok, detail = _check_github_copilot_oauth_non_invasive()
        status["authenticated"] = ok
        status["message"] = detail
        if ok:
            return None, status
        return (
            "GitHub Copilot OAuth token cache is not ready "
            f"({detail}). Run: ppx provider login github-copilot",
            status,
        )

    # Keep unknown OAuth providers non-blocking until a checker is implemented.
    status["authenticated"] = None
    status["message"] = "not_checked_no_provider_checker"
    return None, status


def _doctor_apply_channel_legacy_migrations(
    *,
    channels_cfg: dict[str, Any],
    raw_channels: dict[str, Any],
    changes: list[str],
    skipped: list[str],
    event_sink: list[dict[str, str]] | None = None,
) -> None:
    """Apply channel snake_case -> camelCase migrations from raw config."""
    for channel_name, legacy_key, target_key in doctor_rules.LEGACY_CHANNEL_FIELD_MIGRATIONS:
        channel_cfg = channels_cfg.get(channel_name, {})
        raw_channel_cfg = raw_channels.get(channel_name, {})
        if not isinstance(channel_cfg, dict) or not isinstance(raw_channel_cfg, dict):
            continue
        if str(channel_cfg.get(target_key, "")).strip():
            _doctor_add_skipped(
                skipped,
                event_sink=event_sink,
                code="channel.legacy.target_already_set",
                rule="channel_legacy_migration",
                message=f"channels.{channel_name}.{target_key} already set",
            )
            continue
        legacy_value = raw_channel_cfg.get(legacy_key)
        if not str(legacy_value or "").strip():
            _doctor_add_skipped(
                skipped,
                event_sink=event_sink,
                code="channel.legacy.source_empty",
                rule="channel_legacy_migration",
                message=f"channels.{channel_name}.{legacy_key} empty",
            )
            continue
        channel_cfg[target_key] = str(legacy_value).strip()
        _doctor_add_change(
            changes,
            event_sink=event_sink,
            code="channel.legacy.migrated",
            rule="channel_legacy_migration",
            message=f"channels.{channel_name}.{target_key} <- channels.{channel_name}.{legacy_key}",
        )


def _doctor_backfill_channel_fields_from_env(
    *,
    channels_cfg: dict[str, Any],
    changes: list[str],
    skipped: list[str],
    event_sink: list[dict[str, str]] | None = None,
) -> None:
    """Backfill enabled channel fields from environment variables."""
    for backfill_rule in doctor_rules.DOCTOR_CHANNEL_ENV_BACKFILL_RULES:
        channel_cfg = channels_cfg.get(backfill_rule.channel, {})
        if not isinstance(channel_cfg, dict) or not bool(channel_cfg.get("enabled")):
            _doctor_add_skipped(
                skipped,
                event_sink=event_sink,
                code=backfill_rule.code_disabled,
                rule=backfill_rule.rule,
                message=f"{backfill_rule.target_path} skipped (channel disabled)",
            )
            continue
        if str(channel_cfg.get(backfill_rule.key, "")).strip():
            _doctor_add_skipped(
                skipped,
                event_sink=event_sink,
                code=backfill_rule.code_target_set,
                rule=backfill_rule.rule,
                message=f"{backfill_rule.target_path} already set",
            )
            continue
        env_value = os.getenv(backfill_rule.env_name, "").strip()
        if not env_value:
            _doctor_add_skipped(
                skipped,
                event_sink=event_sink,
                code=backfill_rule.code_source_missing,
                rule=backfill_rule.rule,
                message=f"{backfill_rule.env_name} missing",
            )
            continue
        channel_cfg[backfill_rule.key] = env_value
        _doctor_add_change(
            changes,
            event_sink=event_sink,
            code=backfill_rule.code_applied,
            rule=backfill_rule.rule,
            message=f"{backfill_rule.target_path} <- {backfill_rule.env_name}",
        )


def _doctor_apply_provider_legacy_migrations(
    *,
    providers_cfg: dict[str, Any],
    raw_providers: dict[str, Any],
    changes: list[str],
    skipped: list[str],
    event_sink: list[dict[str, str]] | None = None,
) -> None:
    """Apply provider legacy key migrations from raw config."""
    canonical_names = set(provider_names())
    for raw_name, raw_item in raw_providers.items():
        if not isinstance(raw_item, dict):
            continue
        canonical_name = canonical_provider_name(str(raw_name))
        if canonical_name == str(raw_name) or canonical_name not in canonical_names:
            continue
        target = providers_cfg.get(canonical_name, {})
        if not isinstance(target, dict):
            continue
        if bool(raw_item.get("enabled")) and not bool(target.get("enabled")):
            target["enabled"] = True
            _doctor_add_change(
                changes,
                event_sink=event_sink,
                code="provider.legacy.enabled_migrated",
                rule="provider_legacy_migration",
                message=f"providers.{canonical_name}.enabled <- providers.{raw_name}.enabled",
            )
        elif bool(raw_item.get("enabled")) and bool(target.get("enabled")):
            _doctor_add_skipped(
                skipped,
                event_sink=event_sink,
                code="provider.legacy.enabled_kept",
                rule="provider_legacy_migration",
                message=f"providers.{canonical_name}.enabled kept existing value (source providers.{raw_name}.enabled)",
            )
        for key in ("apiKey", "model", "apiBase"):
            if str(target.get(key, "")).strip():
                _doctor_add_skipped(
                    skipped,
                    event_sink=event_sink,
                    code="provider.legacy.target_already_set",
                    rule="provider_legacy_migration",
                    message=f"providers.{canonical_name}.{key} already set",
                )
                continue
            value = raw_item.get(key)
            if not str(value or "").strip():
                _doctor_add_skipped(
                    skipped,
                    event_sink=event_sink,
                    code="provider.legacy.source_empty",
                    rule="provider_legacy_migration",
                    message=f"providers.{raw_name}.{key} empty",
                )
                continue
            target[key] = str(value).strip()
            _doctor_add_change(
                changes,
                event_sink=event_sink,
                code="provider.legacy.migrated",
                rule="provider_legacy_migration",
                message=f"providers.{canonical_name}.{key} <- providers.{raw_name}.{key}",
            )
        for legacy_key, target_key in doctor_rules.LEGACY_PROVIDER_FIELD_MIGRATIONS:
            if str(target.get(target_key, "")).strip():
                _doctor_add_skipped(
                    skipped,
                    event_sink=event_sink,
                    code="provider.legacy.target_already_set",
                    rule="provider_legacy_migration",
                    message=f"providers.{canonical_name}.{target_key} already set",
                )
                continue
            legacy_value = raw_item.get(legacy_key)
            if not str(legacy_value or "").strip():
                _doctor_add_skipped(
                    skipped,
                    event_sink=event_sink,
                    code="provider.legacy.source_empty",
                    rule="provider_legacy_migration",
                    message=f"providers.{raw_name}.{legacy_key} empty",
                )
                continue
            target[target_key] = str(legacy_value).strip()
            _doctor_add_change(
                changes,
                event_sink=event_sink,
                code="provider.legacy.migrated",
                rule="provider_legacy_migration",
                message=f"providers.{canonical_name}.{target_key} <- providers.{raw_name}.{legacy_key}",
            )

    for raw_name, raw_item in raw_providers.items():
        if not isinstance(raw_item, dict):
            continue
        canonical_name = canonical_provider_name(str(raw_name))
        if canonical_name not in canonical_names:
            continue
        target = providers_cfg.get(canonical_name, {})
        if not isinstance(target, dict):
            continue
        for legacy_key, target_key in doctor_rules.LEGACY_PROVIDER_FIELD_MIGRATIONS:
            if str(target.get(target_key, "")).strip():
                continue
            legacy_value = raw_item.get(legacy_key)
            if not str(legacy_value or "").strip():
                continue
            target[target_key] = str(legacy_value).strip()
            _doctor_add_change(
                changes,
                event_sink=event_sink,
                code="provider.legacy.migrated",
                rule="provider_legacy_migration",
                message=f"providers.{canonical_name}.{target_key} <- providers.{raw_name}.{legacy_key}",
            )


def _doctor_ensure_active_provider(
    *,
    providers_cfg: dict[str, Any],
    changes: list[str],
    event_sink: list[dict[str, str]] | None = None,
) -> str | None:
    """Ensure one provider is enabled and return the active provider name."""
    active_provider = next(
        (
            str(name)
            for name, item in providers_cfg.items()
            if isinstance(item, dict) and bool(item.get("enabled"))
        ),
        None,
    )
    if active_provider is not None:
        return active_provider

    default_provider_cfg = providers_cfg.get(DEFAULT_PROVIDER, {})
    if isinstance(default_provider_cfg, dict):
        default_provider_cfg["enabled"] = True
        _doctor_add_change(
            changes,
            event_sink=event_sink,
            code="provider.default.enabled",
            rule="provider_default_enable",
            message=f"providers.{DEFAULT_PROVIDER}.enabled <- true (doctor default)",
        )
        return DEFAULT_PROVIDER
    return None


def _doctor_ensure_at_least_one_enabled_channel(
    *,
    channels_cfg: dict[str, Any],
    changes: list[str],
    event_sink: list[dict[str, str]] | None = None,
) -> None:
    """Ensure at least one channel is enabled, falling back to local channel."""
    enabled_channels = [
        name
        for name, item in channels_cfg.items()
        if isinstance(item, dict) and bool(item.get("enabled"))
    ]
    if enabled_channels:
        return
    local_cfg = channels_cfg.get("local", {})
    if isinstance(local_cfg, dict):
        local_cfg["enabled"] = True
        _doctor_add_change(
            changes,
            event_sink=event_sink,
            code="channel.default.local_enabled",
            rule="channel_default_enable",
            message="channels.local.enabled <- true (doctor default)",
        )


def _doctor_backfill_email_consent_from_env(
    *,
    channels_cfg: dict[str, Any],
    changes: list[str],
    skipped: list[str],
    event_sink: list[dict[str, str]] | None = None,
) -> None:
    """Backfill email consent flag from environment if email channel is enabled."""
    for bool_rule in doctor_rules.DOCTOR_CHANNEL_BOOL_ENV_BACKFILL_RULES:
        channel_cfg = channels_cfg.get(bool_rule.channel, {})
        if not isinstance(channel_cfg, dict) or not bool(channel_cfg.get("enabled")) or bool(channel_cfg.get(bool_rule.key)):
            continue

        value_raw = os.getenv(bool_rule.env_name, "").strip()
        if value_raw.lower() in set(bool_rule.truthy_values):
            channel_cfg[bool_rule.key] = True
            _doctor_add_change(
                changes,
                event_sink=event_sink,
                code=bool_rule.code_applied,
                rule=bool_rule.rule,
                message=f"{bool_rule.target_path} <- {bool_rule.env_name}",
            )
        elif value_raw:
            _doctor_add_skipped(
                skipped,
                event_sink=event_sink,
                code=bool_rule.code_present_not_truthy,
                rule=bool_rule.rule,
                message=f"{bool_rule.env_name} present but not truthy",
            )
        else:
            _doctor_add_skipped(
                skipped,
                event_sink=event_sink,
                code=bool_rule.code_source_missing,
                rule=bool_rule.rule,
                message=f"{bool_rule.env_name} missing",
            )


def _doctor_backfill_provider_api_key_from_env(
    *,
    providers_cfg: dict[str, Any],
    active_provider: str | None,
    changes: list[str],
    event_sink: list[dict[str, str]] | None = None,
) -> None:
    """Backfill active provider apiKey from env when apiKey is empty."""
    if not active_provider:
        return
    item = providers_cfg.get(active_provider, {})
    if not isinstance(item, dict):
        return

    provider_spec = find_provider_spec(active_provider)
    if provider_spec and provider_spec.is_oauth:
        return
    env_name = provider_api_key_env(active_provider)
    env_value = os.getenv(env_name, "").strip() if env_name else ""
    if env_value and not str(item.get("apiKey", "")).strip():
        item["apiKey"] = env_value
        _doctor_add_change(
            changes,
            event_sink=event_sink,
            code="provider.env.api_key_backfilled",
            rule="provider_env_backfill",
            message=f"providers.{active_provider}.apiKey <- {env_name}",
        )


def _doctor_apply_minimal_fixes(
    config_path: Path,
    *,
    dry_run: bool = False,
    event_sink: list[dict[str, str]] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Apply minimal config fixes from current environment values.

    This is intentionally conservative: only fills missing fields for the
    currently enabled provider/channels when matching env values are already
    present.
    """

    cfg = load_config(config_path=config_path)
    changes: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    raw: dict[str, Any] = {}
    try:
        raw_loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(raw_loaded, dict):
            raw = raw_loaded
    except Exception:
        raw = {}

    providers_cfg = cfg.get("providers", {})
    if isinstance(providers_cfg, dict):
        # Migrate common legacy provider keys (e.g. openai-codex -> openai_codex)
        # from raw config before normalized defaults drop unknown keys.
        raw_providers = raw.get("providers", {}) if isinstance(raw, dict) else {}
        if isinstance(raw_providers, dict):
            _doctor_apply_provider_legacy_migrations(
                providers_cfg=providers_cfg,
                raw_providers=raw_providers,
                changes=changes,
                skipped=skipped,
                event_sink=event_sink,
            )

        active_provider = _doctor_ensure_active_provider(
            providers_cfg=providers_cfg,
            changes=changes,
            event_sink=event_sink,
        )
        _doctor_backfill_provider_api_key_from_env(
            providers_cfg=providers_cfg,
            active_provider=active_provider,
            changes=changes,
            event_sink=event_sink,
        )

    channels_cfg = cfg.get("channels", {})
    raw_channels = raw.get("channels", {}) if isinstance(raw, dict) else {}
    if isinstance(channels_cfg, dict) and isinstance(raw_channels, dict):
        _doctor_apply_channel_legacy_migrations(
            channels_cfg=channels_cfg,
            raw_channels=raw_channels,
            changes=changes,
            skipped=skipped,
            event_sink=event_sink,
        )

    if isinstance(channels_cfg, dict):
        _doctor_ensure_at_least_one_enabled_channel(
            channels_cfg=channels_cfg,
            changes=changes,
            event_sink=event_sink,
        )

        _doctor_backfill_channel_fields_from_env(
            channels_cfg=channels_cfg,
            changes=changes,
            skipped=skipped,
            event_sink=event_sink,
        )

        _doctor_backfill_email_consent_from_env(
            channels_cfg=channels_cfg,
            changes=changes,
            skipped=skipped,
            event_sink=event_sink,
        )

    if changes and not dry_run:
        try:
            save_config(cfg, config_path=config_path)
        except Exception as exc:
            _doctor_add_failed(
                failed,
                event_sink=event_sink,
                code="config.save.failed",
                rule="persist_config",
                message=f"save_config failed: {exc}",
            )
    return changes, skipped, failed


def _doctor_event_summary(events: list[dict[str, str]]) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Build reason-code and per-rule counters from structured doctor events."""
    reason_codes: dict[str, int] = {}
    by_rule: dict[str, dict[str, int]] = {}
    for event in events:
        code = str(event.get("code", "")).strip()
        rule = str(event.get("rule", "")).strip() or "unknown"
        outcome = str(event.get("outcome", "")).strip()
        if code:
            reason_codes[code] = reason_codes.get(code, 0) + 1
        if rule not in by_rule:
            by_rule[rule] = {"applied": 0, "skipped": 0, "failed": 0, "total": 0}
        if outcome in {"applied", "skipped", "failed"}:
            by_rule[rule][outcome] += 1
        by_rule[rule]["total"] += 1
    return reason_codes, by_rule


def _doctor_fix_summary(
    changes: list[str],
    skipped: list[str],
    failed: list[str],
    *,
    events: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build grouped summary for doctor fix changes."""

    grouped: dict[str, list[str]] = {
        "defaults": [],
        "env_backfill": [],
        "legacy_migration": [],
        "other": [],
    }
    for item in changes:
        if "(doctor default)" in item:
            grouped["defaults"].append(item)
            continue
        rhs = item.split("<-", 1)[1].strip() if "<-" in item else ""
        if rhs and rhs.isupper() and "_" in rhs:
            grouped["env_backfill"].append(item)
            continue
        if rhs.startswith("providers.") or rhs.startswith("channels."):
            grouped["legacy_migration"].append(item)
            continue
        grouped["other"].append(item)
    counts = {name: len(items) for name, items in grouped.items()}
    reason_codes: dict[str, int] = {}
    by_rule: dict[str, dict[str, int]] = {}
    if events:
        reason_codes, by_rule = _doctor_event_summary(events)

    return {
        "applied": len(changes),
        "skipped": len(skipped),
        "failed": len(failed),
        "counts": counts,
        "grouped": grouped,
        "skippedItems": skipped,
        "failedItems": failed,
        "reasonCodes": reason_codes,
        "byRule": by_rule,
    }


def _gui_execution_path_hint(
    *,
    builtin_tools_enabled: bool,
    gui_task_tool: str | None,
    gui_action_tool: str | None,
) -> tuple[str, str]:
    """Build doctor-friendly GUI execution mode and hint."""
    gui_mcp_configured = bool(gui_task_tool and gui_action_tool)
    if builtin_tools_enabled and gui_mcp_configured:
        return (
            "hybrid_prefer_mcp",
            (
                "MCP GUI + builtin GUI are both enabled. Runtime should prefer "
                f"`{gui_task_tool}`/`{gui_action_tool}` and keep builtin as fallback."
            ),
        )
    if builtin_tools_enabled and not gui_mcp_configured:
        return (
            "builtin_only",
            "Only builtin GUI tools are enabled. Configure `tools.mcpServers.openpipixia_gui` to move GUI execution into MCP.",
        )
    if (not builtin_tools_enabled) and gui_mcp_configured:
        return ("mcp_only", "Builtin GUI tools are disabled. GUI execution is routed through MCP only.")
    return (
        "disabled",
        "Builtin GUI tools are disabled and no GUI MCP server is configured. GUI actions/tasks are currently unavailable.",
    )


def _cmd_doctor(
    *,
    output_json: bool = False,
    verbose: bool = False,
    fix: bool = False,
    fix_dry_run: bool = False,
    no_color: bool = False,
) -> int:
    """Run runtime diagnostics and return a process exit code.

    When `output_json` is true, emits one machine-readable JSON payload.
    """
    config_path = get_config_path()
    fix_changes: list[str] = []
    fix_skipped: list[str] = []
    fix_failed: list[str] = []
    fix_events: list[dict[str, str]] = []
    if fix:
        fix_changes, fix_skipped, fix_failed = _doctor_apply_minimal_fixes(
            config_path,
            dry_run=fix_dry_run,
            event_sink=fix_events,
        )
        fix_summary = _doctor_fix_summary(fix_changes, fix_skipped, fix_failed, events=fix_events)
        if not output_json:
            if fix_changes:
                for item in fix_changes:
                    _stdout_line(f"Doctor fix {'planned' if fix_dry_run else 'applied'}: {item}")
                _stdout_line(
                    "Doctor fix summary: "
                    f"dry_run={fix_dry_run}, "
                    f"applied={fix_summary['applied']}, "
                    f"skipped={fix_summary['skipped']}, "
                    f"failed={fix_summary['failed']}, "
                    f"defaults={fix_summary['counts']['defaults']}, "
                    f"env_backfill={fix_summary['counts']['env_backfill']}, "
                    f"legacy_migration={fix_summary['counts']['legacy_migration']}, "
                    f"other={fix_summary['counts']['other']}"
                )
                if fix_failed:
                    for item in fix_failed:
                        _stdout_line(f"Doctor fix failed: {item}")
            else:
                _stdout_line("Doctor fix: no config changes applied.")
    else:
        fix_summary = _doctor_fix_summary([], [], [], events=[])

    issues: list[str] = []
    if shutil.which("adk") is None:
        issues.append("Missing `adk` CLI. Install with: pip install google-adk")
    provider_name = normalize_provider_name(os.getenv("OPENPPX_PROVIDER"))
    provider_model = normalize_model_name(provider_name, os.getenv("OPENPPX_MODEL"))
    provider_enabled = env_enabled("OPENPPX_PROVIDER_ENABLED", default=True)
    provider_key_env = provider_api_key_env(provider_name)
    provider_oauth: dict[str, Any] = {"required": False, "authenticated": None, "message": ""}
    if not provider_enabled:
        issues.append("No provider is enabled. Enable one in config (e.g. providers.google.enabled=true).")
    else:
        provider_issue = validate_provider_runtime(provider_name)
        if provider_issue:
            issues.append(provider_issue)
        if provider_key_env and not os.getenv(provider_key_env, "").strip():
            issues.append(
                f"Missing {provider_name} API key. Set `providers.{provider_name}.apiKey` "
                f"in ~/.openppx/config.json or export {provider_key_env}."
            )
        oauth_issue, provider_oauth = _provider_oauth_health(provider_name)
        if oauth_issue:
            issues.append(oauth_issue)

    registry = get_registry()
    skills_count = len(registry.list_skills())
    session_db_url = _doctor_session_db_url()
    if parse_enabled_channels is _parse_enabled_channels:
        configured_channels = _doctor_parse_enabled_channels()
    else:
        # Respect external monkeypatches/injections used by tests/integrations.
        configured_channels = parse_enabled_channels(None)
    should_validate_channels = any(name != "local" for name in configured_channels)
    channel_issues = validate_channel_setup(configured_channels) if should_validate_channels else []
    issues.extend(channel_issues)
    if "whatsapp" in configured_channels and _whatsapp_bridge_precheck_enabled():
        bridge_issue = _check_whatsapp_bridge_ready()
        if bridge_issue:
            issues.append(bridge_issue)
    heartbeat_snapshot = read_heartbeat_status_snapshot(registry.workspace)
    install_prereqs = _install_prereq_lines()
    web_enabled = env_enabled("OPENPPX_WEB_ENABLED", default=True)
    web_search_enabled = env_enabled("OPENPPX_WEB_SEARCH_ENABLED", default=True)
    web_search_provider = os.getenv("OPENPPX_WEB_SEARCH_PROVIDER", "brave").strip().lower() or "brave"
    web_search_key_configured = bool(os.getenv("BRAVE_API_KEY", "").strip())
    security_policy = load_security_policy()
    mcp_toolsets = build_mcp_toolsets_from_env(log_registered=False)
    mcp_summaries = summarize_mcp_toolsets(mcp_toolsets)
    gui_builtin_tools_enabled = env_enabled("OPENPPX_GUI_BUILTIN_TOOLS_ENABLED", default=True)
    gui_mcp = resolve_gui_mcp_from_env() or resolve_gui_mcp_from_summaries(mcp_summaries)
    gui_task_tool = gui_mcp.task_tool_name if gui_mcp else None
    gui_action_tool = gui_mcp.action_tool_name if gui_mcp else None
    gui_mode, gui_hint = _gui_execution_path_hint(
        builtin_tools_enabled=gui_builtin_tools_enabled,
        gui_task_tool=gui_task_tool,
        gui_action_tool=gui_action_tool,
    )
    mcp_probe_policy = _load_mcp_probe_policy(
        timeout_env_name="OPENPPX_MCP_DOCTOR_TIMEOUT_SECONDS",
        timeout_default=5.0,
    )
    mcp_probe_results: list[dict[str, object]] = []
    if mcp_toolsets:
        try:
            mcp_probe_results = asyncio.run(
                probe_mcp_toolsets(
                    mcp_toolsets,
                    timeout_seconds=mcp_probe_policy.timeout_seconds,
                    retry_attempts=mcp_probe_policy.retry_attempts,
                    retry_backoff_seconds=mcp_probe_policy.retry_backoff_seconds,
                )
            )
        except Exception as exc:
            issues.append(f"MCP diagnostics failed: {exc}")
    for result in mcp_probe_results:
        if str(result.get("status")) != "ok":
            name = str(result.get("name", "unknown"))
            status = str(result.get("status", "error"))
            kind = str(result.get("error_kind", "unknown")) or "unknown"
            error = str(result.get("error", "")).strip()
            details = f"{status}/{kind}: {error}" if error else f"{status}/{kind}"
            issues.append(f"MCP server '{name}' health check failed ({details})")

    report: dict[str, Any] = {
        "ok": not issues,
        "issues": list(issues),
        "config": {
            "path": str(config_path),
            "exists": config_path.exists(),
            "workspace": str(registry.workspace),
        },
        "provider": {
            "name": provider_name,
            "enabled": provider_enabled,
            "model": provider_model,
            "oauth": provider_oauth,
        },
        "fix": {
            "applied": bool(fix_changes),
            "dryRun": bool(fix_dry_run),
            "changes": fix_changes,
            "reasonCodes": fix_summary["reasonCodes"],
            "byRule": fix_summary["byRule"],
            "summary": fix_summary,
        },
        "skills": {"count": skills_count},
        "session": {"db_url": session_db_url},
        "channels": {"configured": configured_channels},
        "installPrereqs": install_prereqs,
        "heartbeat": {
            "snapshot_available": heartbeat_snapshot is not None,
            "status": heartbeat_snapshot or {},
        },
        "web": {
            "enabled": web_enabled and web_search_enabled,
            "provider": web_search_provider,
            "api_key_configured": web_search_key_configured,
        },
        "security": {
            "restrict_to_workspace": security_policy.restrict_to_workspace,
            "allow_exec": security_policy.allow_exec,
            "allow_network": security_policy.allow_network,
            "exec_allowlist": list(security_policy.exec_allowlist),
        },
        "mcp": {
            "configured": mcp_summaries,
            "health": mcp_probe_results,
            "probe": {
                "timeout_seconds": mcp_probe_policy.timeout_seconds,
                "retry_attempts": mcp_probe_policy.retry_attempts,
                "retry_backoff_seconds": mcp_probe_policy.retry_backoff_seconds,
            },
        },
        "gui": {
            "builtin_tools_enabled": gui_builtin_tools_enabled,
            "task_tool": gui_task_tool,
            "action_tool": gui_action_tool,
            "mode": gui_mode,
            "hint": gui_hint,
        },
    }

    doctor_debug_lines: list[str] = [
        f"Config file: {config_path}" + (" (found)" if config_path.exists() else " (not found)"),
        f"Workspace: {registry.workspace}",
        f"Detected skills: {skills_count}",
        f"Provider: {provider_name} (enabled={provider_enabled}, model={provider_model})",
        (
            "Provider OAuth: "
            f"required={provider_oauth.get('required')}, "
            f"authenticated={provider_oauth.get('authenticated')}, "
            f"message={provider_oauth.get('message')}"
        ),
        f"Session storage: sqlite ({session_db_url})",
        f"Configured channels: {', '.join(configured_channels) if configured_channels else '(none)'}",
        "Heartbeat status snapshot: " + ("available" if heartbeat_snapshot is not None else "missing"),
        (
            "Web search: "
            f"enabled={web_enabled and web_search_enabled}, "
            f"provider={web_search_provider}, "
            f"api_key={'configured' if web_search_key_configured else 'missing'}"
        ),
        (
            "Security: "
            f"restrict_to_workspace={security_policy.restrict_to_workspace}, "
            f"allow_exec={security_policy.allow_exec}, "
            f"allow_network={security_policy.allow_network}, "
            f"exec_allowlist={list(security_policy.exec_allowlist)}"
        ),
        (
            "GUI execution: "
            f"builtin_tools_enabled={gui_builtin_tools_enabled}, "
            f"mode={gui_mode}, "
            f"hint={gui_hint}"
        ),
    ]
    if not mcp_summaries:
        doctor_debug_lines.append("MCP: no servers configured")
    else:
        doctor_debug_lines.append(f"MCP: configured servers={len(mcp_summaries)}")
        for item in mcp_summaries:
            doctor_debug_lines.append(
                "MCP: "
                f"name={item.get('name')}, "
                f"transport={item.get('transport')}, "
                f"prefix={item.get('prefix')}"
            )
        for result in mcp_probe_results:
            doctor_debug_lines.append(
                "MCP health: "
                f"name={result.get('name')}, "
                f"status={result.get('status')}, "
                f"tools={result.get('tool_count')}, "
                f"elapsed_ms={result.get('elapsed_ms')}, "
                f"attempts={result.get('attempts')}, "
                f"error_kind={result.get('error_kind')}, "
                f"error={result.get('error')}"
            )

    debug_log_path = _append_doctor_debug_lines(doctor_debug_lines)

    if output_json:
        _stdout_line(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1

    color_enabled = (
        (not no_color)
        and bool(getattr(sys.stdout, "isatty", lambda: False)())
        and (not os.getenv("NO_COLOR", "").strip())
    )

    def _color(text: str, code: str) -> str:
        if not color_enabled:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    def _section(title: str) -> str:
        return _color(title, "1;36")

    def _ok(text: str) -> str:
        return _color(text, "32")

    def _warn(text: str) -> str:
        return _color(text, "33")

    def _err(text: str) -> str:
        return _color(text, "31")

    if verbose:
        _stdout_line(_section("Doctor details:"))
        _stdout_line(json.dumps(report, ensure_ascii=False, indent=2))
        if debug_log_path is not None:
            _stdout_line(f"Doctor debug log: {debug_log_path}")

    _stdout_line(_section("Install prerequisites:"))
    for line in install_prereqs:
        normalized = _doctor_install_prereq_line(line)
        if "[warn]:" in normalized:
            _stdout_line(_warn(normalized))
        else:
            _stdout_line(_ok(normalized))

    _stdout_line(_section("Runtime status:"))
    if heartbeat_snapshot is None:
        _stdout_line(_warn("Heartbeat: snapshot=missing"))
    else:
        _stdout_line(
            "Heartbeat: "
            f"last_status={heartbeat_snapshot.get('last_status', '-')}, "
            f"last_reason={heartbeat_snapshot.get('last_reason', '-')}, "
            f"reasons={json.dumps(heartbeat_snapshot.get('recent_reason_counts', {}), ensure_ascii=False)}"
        )
    _stdout_line(
        "GUI runtime: "
        f"mode={gui_mode}, "
        f"builtin_tools_enabled={gui_builtin_tools_enabled}, "
        f"task_tool={gui_task_tool or '-'}, "
        f"action_tool={gui_action_tool or '-'}, "
        f"hint={gui_hint}"
    )

    if issues:
        _stdout_line(_section("Issues:"))
        for item in issues:
            _stdout_line(_err(f"- {item}"))
        return 1

    _stdout_line(_ok("Environment looks good."))
    return 0


def _doctor_install_prereq_line(line: str) -> str:
    """Render one install prereq line with a lightweight health status tag."""

    normalized = str(line).strip()
    if normalized.lower().startswith("install prereq:"):
        normalized = normalized.split(":", 1)[1].strip()
    lower = normalized.lower()
    is_warn = "not found" in lower or "missing" in lower
    level = "warn" if is_warn else "ok"
    return f"Install prereq [{level}]: {normalized}"


_PROVIDER_LOGIN_HANDLERS: dict[str, Callable[[], None]] = {}


def _register_provider_login(provider_name: str) -> Callable[[Callable[[], None]], Callable[[], None]]:
    """Register a provider OAuth login handler."""

    def _decorator(fn: Callable[[], None]) -> Callable[[], None]:
        _PROVIDER_LOGIN_HANDLERS[provider_name] = fn
        return fn

    return _decorator


@_register_provider_login("openai_codex")
def _provider_login_openai_codex() -> None:
    """Authenticate OpenAI Codex with oauth-cli-kit interactive flow."""
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
    except ImportError as exc:
        raise RuntimeError("oauth-cli-kit is not installed. Run: pip install oauth-cli-kit") from exc

    token = None
    try:
        token = get_token()
    except Exception:
        token = None

    has_access = bool(token and getattr(token, "access", ""))
    has_account_id = bool(token and getattr(token, "account_id", ""))
    if not (has_access and has_account_id):
        _stdout_line("Starting interactive OAuth login for OpenAI Codex...")
        token = login_oauth_interactive(
            print_fn=lambda s: _stdout_line(str(s)),
            prompt_fn=lambda s: input(str(s)),
        )

    ok, detail = _check_openai_codex_oauth()
    if not ok:
        raise RuntimeError(f"OpenAI Codex authentication failed ({detail}).")

    account_id = str(getattr(token, "account_id", "")).strip()
    _stdout_line(f"OpenAI Codex OAuth authenticated ({account_id}).")


@_register_provider_login("github_copilot")
def _provider_login_github_copilot() -> None:
    """Authenticate GitHub Copilot via LiteLLM device flow."""
    _stdout_line("Starting GitHub Copilot OAuth device flow...")

    async def _trigger() -> None:
        from litellm import acompletion

        await acompletion(
            model="github_copilot/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )

    asyncio.run(_trigger())
    _stdout_line("GitHub Copilot OAuth authenticated.")


def _cmd_provider_list(*, verbose: bool = False) -> int:
    """List providers known by the runtime."""
    _stdout_line("Providers:")
    for name in provider_names():
        spec = find_provider_spec(name)
        if not spec:
            _stdout_line(f"- {name}")
            continue
        line = f"- {name}: default_model={spec.default_model}"
        if verbose:
            line += f", runtime={spec.runtime}"
            if spec.is_oauth:
                line += ", oauth=true"
        _stdout_line(line)
    return 0


def _cmd_provider_status(*, output_json: bool = False) -> int:
    """Show runtime status for currently selected provider."""
    provider_name = normalize_provider_name(os.getenv("OPENPPX_PROVIDER"))
    provider_model = normalize_model_name(provider_name, os.getenv("OPENPPX_MODEL"))
    provider_enabled = env_enabled("OPENPPX_PROVIDER_ENABLED", default=True)
    provider_key_env = provider_api_key_env(provider_name)
    provider_key_configured = bool(provider_key_env is None or os.getenv(provider_key_env, "").strip())

    issues: list[str] = []
    provider_issue = ""
    provider_oauth: dict[str, Any] = {"required": False, "authenticated": None, "message": ""}

    if not provider_enabled:
        issues.append("No provider is enabled.")
    else:
        provider_issue = validate_provider_runtime(provider_name) or ""
        if provider_issue:
            issues.append(provider_issue)
        if provider_key_env and not provider_key_configured:
            issues.append(f"Missing API key env: {provider_key_env}")
        oauth_issue, provider_oauth = _provider_oauth_health(provider_name)
        if oauth_issue:
            issues.append(oauth_issue)

    report: dict[str, Any] = {
        "ok": not issues,
        "issues": issues,
        "provider": {
            "name": provider_name,
            "enabled": provider_enabled,
            "model": provider_model,
            "runtime_issue": provider_issue,
            "api_key_env": provider_key_env,
            "api_key_configured": provider_key_configured,
            "oauth": provider_oauth,
        },
    }

    if output_json:
        _stdout_line(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1

    _stdout_line(
        f"Provider: {provider_name} (enabled={provider_enabled}, model={provider_model})"
    )
    if provider_key_env:
        _stdout_line(f"API key: {provider_key_env}={'configured' if provider_key_configured else 'missing'}")
    else:
        _stdout_line("API key: not required")
    _stdout_line(
        "OAuth: "
        f"required={provider_oauth.get('required')}, "
        f"authenticated={provider_oauth.get('authenticated')}, "
        f"message={provider_oauth.get('message')}"
    )
    if issues:
        _stdout_line("Issues:")
        for issue in issues:
            _stdout_line(f"- {issue}")
        return 1
    _stdout_line("Provider is ready.")
    return 0


def _cmd_provider_login(provider_name: str) -> int:
    """Authenticate an OAuth provider account for local runtime use."""
    normalized = canonical_provider_name(provider_name)
    spec = find_provider_spec(normalized)
    oauth_names = ", ".join(name.replace("_", "-") for name in oauth_provider_names())
    if spec is None or not spec.is_oauth:
        _stdout_line(
            f"Unknown OAuth provider '{provider_name}'. "
            f"Supported providers: {oauth_names}"
        )
        return 1

    handler = _PROVIDER_LOGIN_HANDLERS.get(spec.name)
    if handler is None:
        _stdout_line(f"OAuth login is not implemented for provider '{provider_name}'.")
        return 1

    try:
        handler()
    except Exception as exc:
        _stdout_line(f"OAuth login failed for {provider_name}: {exc}")
        return 1
    return 0


def _dispatch_provider_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch provider subcommands from parsed argparse namespace."""
    list_verbose = bool(getattr(args, "verbose", False) or getattr(args, "debug", False))
    handlers: dict[str, Callable[[], int]] = {
        "list": lambda: _cmd_provider_list(verbose=list_verbose),
        "status": lambda: _cmd_provider_status(output_json=args.output_json),
        "login": lambda: _cmd_provider_login(args.provider_name),
    }
    handler = handlers.get(args.provider_command)
    if handler is None:
        parser.error("provider command is required")
    return handler()


def _bridge_base_dir() -> Path:
    """Return user-local bridge directory root."""
    return get_data_dir() / "bridge"


def _bridge_runtime_state_path() -> Path:
    """Return persisted bridge runtime-state file path."""
    return _bridge_base_dir() / "runtime_state.json"


def _read_bridge_runtime_state() -> dict[str, Any] | None:
    """Read bridge runtime state from disk."""
    path = _bridge_runtime_state_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _write_bridge_runtime_state(payload: dict[str, Any]) -> None:
    """Persist bridge runtime state to disk."""
    path = _bridge_runtime_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _clear_bridge_runtime_state() -> None:
    """Delete persisted bridge runtime state file."""
    path = _bridge_runtime_state_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _state_pid(state: dict[str, Any]) -> int | None:
    """Extract validated process id from runtime-state payload."""
    raw_pid = state.get("pid")
    try:
        pid = int(raw_pid)
    except Exception:
        return None
    if pid <= 0:
        return None
    return pid


def _is_pid_running(pid: int) -> bool:
    """Return whether process id appears alive."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _resolve_bridge_source_dir() -> Path:
    """Resolve WhatsApp bridge source directory containing package.json."""
    override = os.getenv("OPENPPX_WHATSAPP_BRIDGE_SOURCE", "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())

    package_bridge = Path(__file__).resolve().parent / "bridge"
    monorepo_bridge = Path(__file__).resolve().parents[2] / "openpipixia" / "bridge"
    candidates.extend([package_bridge, monorepo_bridge])

    for candidate in candidates:
        if (candidate / "package.json").exists():
            return candidate
    raise RuntimeError(
        "WhatsApp bridge source not found. "
        "Set OPENPPX_WHATSAPP_BRIDGE_SOURCE or include openpipixia/bridge resources."
    )


def _get_bridge_dir() -> Path:
    """Ensure local bridge runtime directory exists and return it."""
    user_bridge = _bridge_base_dir()
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    if shutil.which("npm") is None:
        raise RuntimeError("npm not found. Please install Node.js >= 20.")

    source = _resolve_bridge_source_dir()
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    try:
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr_text = ""
        if isinstance(exc.stderr, bytes):
            stderr_text = exc.stderr.decode(errors="ignore")
        elif isinstance(exc.stderr, str):
            stderr_text = exc.stderr
        stderr_text = stderr_text.strip()
        if stderr_text:
            raise RuntimeError(f"Bridge build failed: {stderr_text[:500]}") from exc
        raise RuntimeError(f"Bridge build failed: {exc}") from exc
    return user_bridge


def _whatsapp_bridge_token_from_config(config: dict[str, Any]) -> str:
    """Extract WhatsApp bridge token from config payload."""
    channels = config.get("channels", {})
    if not isinstance(channels, dict):
        return ""
    whatsapp = channels.get("whatsapp", {})
    if not isinstance(whatsapp, dict):
        return ""
    return str(whatsapp.get("bridgeToken", "")).strip()


def _weixin_channel_from_config(config: dict[str, Any]):
    """Build a Weixin channel instance from config for interactive login."""
    from ..bus.queue import MessageBus
    from ..channels.weixin import WeixinChannel

    channels = config.get("channels", {})
    if not isinstance(channels, dict):
        channels = {}
    section = channels.get("weixin", {})
    if not isinstance(section, dict):
        section = {}
    return WeixinChannel(
        bus=MessageBus(),
        allow_from=[str(item).strip() for item in section.get("allowFrom", []) if str(item).strip()],
        base_url=str(section.get("baseUrl", "https://ilinkai.weixin.qq.com")).strip() or "https://ilinkai.weixin.qq.com",
        token=str(section.get("token", "")).strip(),
        state_dir=str(section.get("stateDir", "")).strip(),
        poll_timeout_seconds=int(section.get("pollTimeoutSeconds", 35) or 35),
    )


def _cmd_channels_login(*, channel_name: str) -> int:
    """Start channel-specific interactive login helpers."""
    target = channel_name.strip().lower()
    if target == "weixin":
        try:
            channel = _weixin_channel_from_config(load_config())
            ok = asyncio.run(channel.login())
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            _stdout_line(f"Weixin login failed: {exc}")
            return 1
        if ok:
            _stdout_line("Weixin login completed. Token was saved for later gateway runs.")
            return 0
        _stdout_line("Weixin login failed.")
        return 1
    if target != "whatsapp":
        _stdout_line(f"Unsupported channel for login: {channel_name}. Supported: whatsapp, weixin")
        return 1

    try:
        bridge_dir = _get_bridge_dir()
    except RuntimeError as exc:
        _stdout_line(str(exc))
        return 1
    except Exception as exc:
        _stdout_line(f"Failed to prepare bridge directory: {exc}")
        return 1

    env = dict(os.environ)
    bridge_token = _whatsapp_bridge_token_from_config(load_config())
    if bridge_token:
        env["BRIDGE_TOKEN"] = bridge_token

    _stdout_line("Starting WhatsApp bridge. Scan the QR code in this terminal to connect.")
    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as exc:
        _stdout_line(f"Bridge failed: {exc}")
        return 1
    except FileNotFoundError:
        _stdout_line("npm not found. Please install Node.js >= 20.")
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


def _stop_bridge_pid(pid: int, *, timeout_seconds: float = 8.0) -> bool:
    """Terminate one bridge process id and wait until it exits."""
    if not _is_pid_running(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False

    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    while time.monotonic() < deadline:
        if not _is_pid_running(pid):
            return True
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False

    # Short grace period after SIGKILL.
    grace_deadline = time.monotonic() + 2.0
    while time.monotonic() < grace_deadline:
        if not _is_pid_running(pid):
            return True
        time.sleep(0.1)
    return not _is_pid_running(pid)


def _cmd_channels_bridge_start(*, channel_name: str) -> int:
    """Start channel bridge in background process."""
    target = channel_name.strip().lower()
    if target != "whatsapp":
        _stdout_line(f"Unsupported channel bridge: {channel_name}. Supported: whatsapp")
        return 1

    state = _read_bridge_runtime_state()
    if state:
        existing_pid = _state_pid(state)
        if existing_pid and _is_pid_running(existing_pid):
            _stdout_line(f"Bridge is already running (pid={existing_pid}).")
            return 0
        _clear_bridge_runtime_state()

    try:
        bridge_dir = _get_bridge_dir()
    except RuntimeError as exc:
        _stdout_line(str(exc))
        return 1
    except Exception as exc:
        _stdout_line(f"Failed to prepare bridge directory: {exc}")
        return 1

    env = dict(os.environ)
    bridge_token = _whatsapp_bridge_token_from_config(load_config())
    if bridge_token:
        env["BRIDGE_TOKEN"] = bridge_token

    log_path = _bridge_base_dir() / "bridge.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("ab") as log_fp:
            proc = subprocess.Popen(
                ["npm", "start"],
                cwd=bridge_dir,
                env=env,
                stdout=log_fp,
                stderr=log_fp,
                start_new_session=True,
            )
    except FileNotFoundError:
        _stdout_line("npm not found. Please install Node.js >= 20.")
        return 1
    except Exception as exc:
        _stdout_line(f"Failed to start bridge: {exc}")
        return 1

    started_at_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    _write_bridge_runtime_state(
        {
            "channel": target,
            "pid": proc.pid,
            "started_at_ms": started_at_ms,
            "bridge_dir": str(bridge_dir),
            "log_path": str(log_path),
        }
    )
    _stdout_line(f"Bridge started in background (pid={proc.pid}).")
    return 0


def _cmd_channels_bridge_status(*, channel_name: str) -> int:
    """Print bridge runtime status."""
    target = channel_name.strip().lower()
    if target != "whatsapp":
        _stdout_line(f"Unsupported channel bridge: {channel_name}. Supported: whatsapp")
        return 1

    state = _read_bridge_runtime_state()
    if not state:
        _stdout_line("Bridge is not running (no runtime state).")
        return 0

    pid = _state_pid(state)
    if pid and _is_pid_running(pid):
        _stdout_line(f"Bridge is running (pid={pid}).")
        return 0
    _stdout_line("Bridge is not running (stale runtime state found).")
    return 0


def _cmd_channels_bridge_stop(*, channel_name: str) -> int:
    """Stop background bridge process tracked by runtime state."""
    target = channel_name.strip().lower()
    if target != "whatsapp":
        _stdout_line(f"Unsupported channel bridge: {channel_name}. Supported: whatsapp")
        return 1

    state = _read_bridge_runtime_state()
    if not state:
        _stdout_line("Bridge is not running.")
        return 0

    pid = _state_pid(state)
    if pid is None:
        _clear_bridge_runtime_state()
        _stdout_line("Bridge runtime state was invalid and has been cleared.")
        return 0

    if not _is_pid_running(pid):
        _clear_bridge_runtime_state()
        _stdout_line(f"Bridge is not running (stale pid={pid} removed).")
        return 0

    if not _stop_bridge_pid(pid):
        _stdout_line(f"Failed to stop bridge process pid={pid}.")
        return 1

    _clear_bridge_runtime_state()
    _stdout_line(f"Bridge stopped (pid={pid}).")
    return 0


def _whatsapp_bridge_precheck_enabled() -> bool:
    """Return whether WhatsApp bridge precheck is enabled."""
    return env_enabled("OPENPPX_WHATSAPP_BRIDGE_PRECHECK", default=True)


def _check_whatsapp_bridge_ready() -> str | None:
    """Verify WhatsApp bridge endpoint is reachable before runtime startup."""
    bridge_url = os.getenv("WHATSAPP_BRIDGE_URL", "").strip()
    if not bridge_url:
        return "Missing WHATSAPP_BRIDGE_URL for whatsapp channel."

    parsed = urlparse(bridge_url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        return (
            f"Invalid WHATSAPP_BRIDGE_URL '{bridge_url}'. "
            "Expected ws://host:port or wss://host:port."
        )

    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=1.5):
            pass
    except OSError as exc:
        return (
            f"WhatsApp bridge is unreachable at {bridge_url} ({exc}). "
            "Run: ppx channels bridge start"
        )
    return None


def _dispatch_channels_bridge_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch channels bridge subcommands."""
    handlers: dict[str, Callable[[], int]] = {
        "start": lambda: _cmd_channels_bridge_start(channel_name=args.channel_name),
        "status": lambda: _cmd_channels_bridge_status(channel_name=args.channel_name),
        "stop": lambda: _cmd_channels_bridge_stop(channel_name=args.channel_name),
    }
    handler = handlers.get(args.channels_bridge_command)
    if handler is None:
        parser.print_help()
        return 2
    return handler()


def _dispatch_channels_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch channels subcommands from parsed argparse namespace."""
    handlers: dict[str, Callable[[], int]] = {
        "login": lambda: _cmd_channels_login(channel_name=args.channel_name),
        "bridge": lambda: _dispatch_channels_bridge_command(args, parser),
    }
    handler = handlers.get(args.channels_command)
    if handler is None:
        parser.print_help()
        return 2
    return handler()


def _cmd_run(passthrough_args: list[str]) -> int:
    if shutil.which("adk") is None:
        _stdout_line("`adk` CLI not found. Install with: pip install google-adk")
        return 1

    agent_dir = Path(__file__).parent.resolve()
    cmd = ["adk", "run", str(agent_dir), *passthrough_args]
    return subprocess.call(cmd)


_INSTALL_EMBEDDED_INIT_SETUP = False
_INIT_DEFAULT_AGENT_NAMES: tuple[str, str, str] = ("agent_name_1", "agent_name_2", "agent_name_3")
_INIT_BOOTSTRAP_TEMPLATES: dict[str, str] = {
    "AGENTS.md": "# AGENTS\n\nDescribe coding/runtime constraints for this agent.\n",
    "SOUL.md": "# SOUL\n\nDescribe personality, tone, and decision style.\n",
    "TOOLS.md": "# TOOLS\n\nList allowed tools and usage boundaries.\n",
    "IDENTITY.md": "# IDENTITY\n\nDescribe the role, owner, and responsibilities of this agent.\n",
    "USER.md": "# USER\n\nDescribe user profile and collaboration preferences.\n",
    "HEARTBEAT.md": (
        "# HEARTBEAT\n\n"
        "Write periodic background tasks here.\n"
        "- One task per bullet\n"
        "- Keep each task concrete and actionable\n"
    ),
}
_INIT_MEMORY_FILES: dict[str, str] = {
    "MEMORY.md": "# Long-term Memory\n\nFacts and preferences extracted from conversations.\n",
    "HISTORY.md": "# Conversation History\n\nAppend-only interaction transcript.\n",
}


@dataclass(frozen=True)
class InstallInitResult:
    """Resolved install init side effects for config/runtime/workspace paths."""

    config_path: Path
    runtime_config_path: Path
    workspace: Path
    config_state: str
    runtime_state: str


@dataclass(frozen=True)
class CreateAgentResult:
    """Resolved side effects after creating one agent."""

    agent_name: str
    role: str
    agent_home: Path
    config_path: Path
    runtime_config_path: Path
    workspace: Path
    global_config_path: Path


def _run_install_init_setup(*, force: bool) -> InstallInitResult:
    """Create/refresh config + runtime config + workspace and return summary."""
    config_path = get_config_path()
    runtime_config_path = get_runtime_config_path()
    existed = config_path.exists()
    runtime_existed = runtime_config_path.exists()

    if force or not existed:
        config = default_config()
        saved_to = save_config(config, config_path=config_path)
        config_state = "reset to defaults" if force and existed else "created"
    else:
        config = load_config(config_path=config_path)
        saved_to = save_config(config, config_path=config_path)
        config_state = "refreshed"

    if force or not runtime_existed:
        runtime_config = default_runtime_config()
        runtime_saved_to = save_runtime_config(runtime_config, runtime_config_path=runtime_config_path)
        runtime_state = "reset to defaults" if force and runtime_existed else "created"
    else:
        runtime_config = load_runtime_config(runtime_config_path=runtime_config_path)
        runtime_saved_to = save_runtime_config(runtime_config, runtime_config_path=runtime_config_path)
        runtime_state = "refreshed"

    workspace = Path(str(config.get("agent", {}).get("workspace", ""))).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    return InstallInitResult(
        config_path=saved_to,
        runtime_config_path=runtime_saved_to,
        workspace=workspace,
        config_state=config_state,
        runtime_state=runtime_state,
    )


def _render_install_init_compact_with_rich(result: InstallInitResult) -> bool:
    """Render install init summary card for install-embedded setup."""
    if not sys.stdout.isatty():
        return False
    try:
        from rich.console import Console  # type: ignore
        from rich.panel import Panel  # type: ignore
        from rich.table import Table  # type: ignore
    except Exception:
        return False

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("key", style="bold cyan", no_wrap=True)
    table.add_column("value", style="white")
    table.add_row("Config", f"{result.config_state}: {result.config_path}")
    table.add_row("Runtime", f"{result.runtime_state}: {result.runtime_config_path}")
    table.add_row("Workspace", str(result.workspace))
    Console().print(Panel(table, title="[bold]Setup Result[/bold]", border_style="cyan"))
    return True


def _cmd_install_init_setup(force: bool) -> int:
    result = _run_install_init_setup(force=force)
    _init_agent_support_files(agent_home=result.config_path.parent, force=force)
    if _INSTALL_EMBEDDED_INIT_SETUP:
        if not _render_install_init_compact_with_rich(result):
            _stdout_line(f"Config {result.config_state}: {result.config_path}")
            _stdout_line(f"Runtime config {result.runtime_state}: {result.runtime_config_path}")
            _stdout_line(f"Workspace ready: {result.workspace}")
        return 0

    print(f"Config {result.config_state}: {result.config_path}")
    print(f"Runtime config {result.runtime_state}: {result.runtime_config_path}")
    print(f"Workspace ready: {result.workspace}")
    print("Next steps:")
    print(f"1. Edit config: {result.config_path}")
    print(f"2. Optional advanced tuning: {result.runtime_config_path}")
    print("3. Configure providers/channels/web sections and their `enabled` flags")
    print("4. Fill providers.<provider>.apiKey for the enabled provider (and channel credentials if needed)")
    print("5. Start gateway: ppx gateway")
    print("6. Dry run: ppx doctor")
    return 0


def _run_multi_agent_init_setup(*, force: bool) -> list[tuple[str, InstallInitResult]]:
    """Create/refresh three agent config/runtime/workspace sets."""
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, InstallInitResult]] = []
    for agent_name in _INIT_DEFAULT_AGENT_NAMES:
        config_path = _agent_config_path(agent_name)
        runtime_config_path = config_path.with_name("runtime.json")
        existed = config_path.exists()
        runtime_existed = runtime_config_path.exists()

        if force or not existed:
            config = default_config()
            config["agent"]["workspace"] = str((data_dir / agent_name / "workspace").expanduser())
            saved_to = save_config(config, config_path=config_path)
            config_state = "reset to defaults" if force and existed else "created"
        else:
            config = load_config(config_path=config_path)
            config.setdefault("agent", {})
            config["agent"]["workspace"] = str((data_dir / agent_name / "workspace").expanduser())
            saved_to = save_config(config, config_path=config_path)
            config_state = "refreshed"

        if force or not runtime_existed:
            runtime_cfg = default_runtime_config()
            runtime_saved_to = save_runtime_config(runtime_cfg, runtime_config_path=runtime_config_path)
            runtime_state = "reset to defaults" if force and runtime_existed else "created"
        else:
            runtime_cfg = load_runtime_config(runtime_config_path=runtime_config_path)
            runtime_saved_to = save_runtime_config(runtime_cfg, runtime_config_path=runtime_config_path)
            runtime_state = "refreshed"

        workspace = Path(str(config.get("agent", {}).get("workspace", ""))).expanduser()
        workspace.mkdir(parents=True, exist_ok=True)

        results.append(
            (
                agent_name,
                InstallInitResult(
                    config_path=saved_to,
                    runtime_config_path=runtime_saved_to,
                    workspace=workspace,
                    config_state=config_state,
                    runtime_state=runtime_state,
                ),
            )
        )
    return results


def _write_text_if_missing(*, path: Path, content: str, force: bool) -> None:
    """Write one UTF-8 text file once, or overwrite when force is true."""
    if path.exists() and not force:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _init_agent_support_files(*, agent_home: Path, force: bool) -> None:
    """Initialize bootstrap, skills, and memory files under one agent home."""
    for name, content in _INIT_BOOTSTRAP_TEMPLATES.items():
        _write_text_if_missing(path=agent_home / name, content=content, force=force)
    (agent_home / "skills").mkdir(parents=True, exist_ok=True)
    memory_dir = agent_home / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    for name, content in _INIT_MEMORY_FILES.items():
        _write_text_if_missing(path=memory_dir / name, content=content, force=force)


def _load_global_config_entries() -> list[dict[str, Any]]:
    """Load normalized global agent entries from global_config.json."""
    path = _global_config_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []

    agents_raw = raw.get("agents")
    entries: list[Any] = []
    if isinstance(agents_raw, list):
        entries = agents_raw
    elif isinstance(agents_raw, dict) and isinstance(agents_raw.get("list"), list):
        entries = agents_raw.get("list", [])

    normalized_entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in entries:
        name = ""
        enabled = True
        if isinstance(item, str):
            name = _normalize_agent_name(item)
        elif isinstance(item, dict):
            name = _normalize_agent_name(item.get("name") or item.get("id"))
            enabled = is_enabled(item.get("enabled"), default=True)
        if not name or name in seen:
            continue
        seen.add(name)
        normalized_entries.append({"name": name, "enabled": enabled})
    return normalized_entries


def _save_global_config_entries(entries: list[dict[str, Any]]) -> Path:
    """Persist global agent entries into global_config.json."""
    path = _global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agents": entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _ensure_global_agent_enabled(agent_name: str) -> Path:
    """Ensure one agent exists in global_config.json and is enabled."""
    entries = _load_global_config_entries()
    for entry in entries:
        if entry["name"] == agent_name:
            entry["enabled"] = True
            return _save_global_config_entries(entries)
    entries.append({"name": agent_name, "enabled": True})
    return _save_global_config_entries(entries)


def _set_global_agent_enabled(*, agent_name: str, enabled: bool) -> tuple[Path, bool]:
    """Set one agent enablement flag in global_config.json.

    Returns the saved global config path and whether the target agent existed
    before the update.
    """
    normalized_name = _normalize_agent_name(agent_name)
    entries = _load_global_config_entries()
    found = False
    for entry in entries:
        if entry["name"] != normalized_name:
            continue
        entry["enabled"] = enabled
        found = True
        break
    if not found:
        entries.append({"name": normalized_name, "enabled": enabled})
    return _save_global_config_entries(entries), found


def _remove_global_agent_entry(agent_name: str) -> Path:
    """Remove one agent entry from global_config.json and persist the result."""
    normalized_name = _normalize_agent_name(agent_name)
    entries = [entry for entry in _load_global_config_entries() if entry["name"] != normalized_name]
    return _save_global_config_entries(entries)


def _default_agent_workspace(agent_name: str) -> Path:
    """Create and return the default tmp workspace for one new agent."""
    return Path(tempfile.mkdtemp(prefix=f"openpipixia-{agent_name}-")).resolve()


def _run_create_agent_setup(*, name: str, role: str, workspace: str | None) -> CreateAgentResult:
    """Create one role-based agent config/runtime/workspace set."""
    normalized_name = _normalize_agent_name(name)
    if not normalized_name:
        raise ValueError("agent name is empty after normalization")

    agent_dir = get_data_dir() / normalized_name
    if agent_dir.exists():
        raise FileExistsError(f"agent '{normalized_name}' already exists: {agent_dir}")

    role_name = normalize_agent_role(role)
    workspace_path = (
        Path(str(workspace).strip()).expanduser().resolve(strict=False)
        if str(workspace or "").strip()
        else _default_agent_workspace(normalized_name)
    )

    config = default_config()
    config["agent"]["name"] = normalized_name
    config["agent"]["workspace"] = str(workspace_path)
    apply_agent_role_defaults(config, role=role_name)

    config_path = _agent_config_path(normalized_name)
    runtime_config_path = config_path.with_name("runtime.json")
    agent_home = config_path.parent
    saved_to = save_config(config, config_path=config_path)
    runtime_saved_to = save_runtime_config(default_runtime_config(), runtime_config_path=runtime_config_path)

    workspace_path.mkdir(parents=True, exist_ok=True)
    _init_agent_support_files(agent_home=agent_home, force=False)
    global_config_path = _ensure_global_agent_enabled(normalized_name)
    return CreateAgentResult(
        agent_name=normalized_name,
        role=role_name,
        agent_home=agent_home,
        config_path=saved_to,
        runtime_config_path=runtime_saved_to,
        workspace=workspace_path,
        global_config_path=global_config_path,
    )


def _cmd_create(*, name: str, role: str, workspace: str | None) -> int:
    """Create one role-based agent and enable it in global_config.json."""
    try:
        result = _run_create_agent_setup(name=name, role=role, workspace=workspace)
    except ValueError as exc:
        _stdout_line(f"Error: {exc}")
        return 2
    except FileExistsError as exc:
        _stdout_line(f"Error: {exc}")
        return 1

    _stdout_line(f"Created agent: {result.agent_name}")
    _stdout_line(f"Role: {result.role}")
    _stdout_line(f"Agent home ready: {result.agent_home}")
    _stdout_line(f"Config created: {result.config_path}")
    _stdout_line(f"Runtime config created: {result.runtime_config_path}")
    _stdout_line(f"Workspace ready: {result.workspace}")
    _stdout_line(f"Global config updated: {result.global_config_path}")
    _stdout_line(f"Agent enabled in global_config.json: {result.agent_name}")
    cfg = load_config(config_path=result.config_path)
    permissions = cfg.get("agent", {}).get("permissions", {})
    if isinstance(permissions, dict):
        _stdout_line("Permission summary:")
        _stdout_line(
            "  "
            + ", ".join(
                [
                    f"filesystem={permissions.get('filesystemAccess', '')}",
                    f"shell={permissions.get('shellExec', '')}",
                    f"network={permissions.get('networkAccess', '')}",
                    f"delegate={permissions.get('canDelegate', False)}",
                    f"high_risk={permissions.get('highRiskActionAccess', '')}",
                ]
            )
        )
    _stdout_line("Next steps:")
    _stdout_line("1. Edit the per-agent config/runtime/agent-home files as needed.")
    _stdout_line("2. ppx doctor  # validate current runtime config")
    _stdout_line(
        "3. ppx --config-path "
        f"{result.config_path} "
        "gateway run --channels local --interactive-local"
    )
    return 0


def _write_init_global_config() -> Path:
    """Write multi-agent global config with only the first agent enabled."""
    path = _global_config_path()
    payload = {
        "agents": [
            {"name": agent_name, "enabled": idx == 0}
            for idx, agent_name in enumerate(_INIT_DEFAULT_AGENT_NAMES)
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _cmd_init(*, force: bool) -> int:
    """Initialize default multi-agent config layout."""
    results = _run_multi_agent_init_setup(force=force)
    global_config_path = _write_init_global_config()

    if _render_init_sections_with_rich(results=results, global_config_path=global_config_path, force=force):
        return 0

    _stdout_line(f"Initialized multi-agent config: agents={len(results)}")
    for agent_name, result in results:
        _init_agent_support_files(agent_home=result.config_path.parent, force=force)
        _stdout_line(f"[agent={agent_name}] Config {result.config_state}: {result.config_path}")
        _stdout_line(f"[agent={agent_name}] Runtime config {result.runtime_state}: {result.runtime_config_path}")
        _stdout_line(f"[agent={agent_name}] Workspace ready: {result.workspace}")
        _stdout_line(
            f"[agent={agent_name}] Bootstrap files initialized: {', '.join(sorted(_INIT_BOOTSTRAP_TEMPLATES.keys()))}"
        )
        _stdout_line(f"[agent={agent_name}] Agent memory initialized: memory/MEMORY.md, memory/HISTORY.md")

    _stdout_line(f"Global config updated: {global_config_path}")
    _stdout_line(f"Enabled agent in global_config.json: {_INIT_DEFAULT_AGENT_NAMES[0]}")
    _stdout_line("You can edit global_config.json to enable/disable agents.")
    _stdout_line("You can edit each agent config/runtime/workspace file under ~/.openppx/<agent_name>/.")
    _stdout_line("Bootstrap file purposes:")
    _stdout_line("- AGENTS.md: agent engineering rules and execution constraints.")
    _stdout_line("- SOUL.md: personality and behavior style guidance.")
    _stdout_line("- TOOLS.md: tool usage policy and guardrails.")
    _stdout_line("- IDENTITY.md: role definition and responsibility boundaries.")
    _stdout_line("- USER.md: user profile and collaboration preferences.")
    _stdout_line("- HEARTBEAT.md: periodic tasks to execute during heartbeat runs.")
    _stdout_line("- skills/: place per-agent local skills (each skill as <name>/SKILL.md).")
    _stdout_line("Next steps:")
    _stdout_line("1. Configure related config files (global_config.json and per-agent config/runtime/agent-home files).")
    _stdout_line("2. ppx doctor  # check whether current config is valid and runnable")
    _stdout_line(
        "3. ppx --config-path "
        f"{str(_agent_config_path(_INIT_DEFAULT_AGENT_NAMES[0]))} "
        "gateway run --channels local --interactive-local "
        "# try local interactive experience first"
    )
    _stdout_line("4. ppx gateway start  # start real background gateway service journey")
    return 0


def _render_init_sections_with_rich(
    *,
    results: list[tuple[str, InstallInitResult]],
    global_config_path: Path,
    force: bool,
) -> bool:
    """Render init output in rich format when terminal supports it."""
    if not sys.stdout.isatty():
        return False
    try:
        from rich.console import Console  # type: ignore
        from rich.panel import Panel  # type: ignore
        from rich.table import Table  # type: ignore
    except Exception:
        return False

    for _agent_name, result in results:
        _init_agent_support_files(agent_home=result.config_path.parent, force=force)

    console = Console()

    summary = Table(show_header=False, box=None, pad_edge=False)
    summary.add_column("key", style="bold cyan", no_wrap=True)
    summary.add_column("value", style="white")
    summary.add_row("Agents initialized", str(len(results)))
    summary.add_row("Global config", str(global_config_path))
    summary.add_row("Enabled by default", _INIT_DEFAULT_AGENT_NAMES[0])
    summary.add_row("Editable", "global_config.json + each agent config/runtime/agent-home files")
    console.print(Panel(summary, title="[bold]Openpipixia Init[/bold]", border_style="cyan"))

    detail = Table(show_header=True, box=None, pad_edge=False)
    detail.add_column("Agent", style="bold green")
    detail.add_column("Config")
    detail.add_column("Runtime")
    detail.add_column("Workspace")
    for agent_name, result in results:
        detail.add_row(
            agent_name,
            str(result.config_path),
            str(result.runtime_config_path),
            str(result.workspace),
        )
    console.print(Panel(detail, title="[bold]Per-agent Files[/bold]", border_style="green"))

    purpose = Table(show_header=True, box=None, pad_edge=False)
    purpose.add_column("File/Dir", style="bold yellow", no_wrap=True)
    purpose.add_column("Purpose")
    purpose.add_row("AGENTS.md", "Agent engineering rules and execution constraints.")
    purpose.add_row("SOUL.md", "Personality and behavior style guidance.")
    purpose.add_row("TOOLS.md", "Tool usage policy and guardrails.")
    purpose.add_row("IDENTITY.md", "Role definition and responsibility boundaries.")
    purpose.add_row("USER.md", "User profile and collaboration preferences.")
    purpose.add_row("HEARTBEAT.md", "Periodic tasks to execute during heartbeat runs.")
    purpose.add_row("skills/", "Per-agent local skills (<name>/SKILL.md).")
    purpose.add_row("memory/MEMORY.md", "Long-term facts and preferences.")
    purpose.add_row("memory/HISTORY.md", "Append-only interaction transcript.")
    console.print(Panel(purpose, title="[bold]Agent Home Files[/bold]", border_style="yellow"))

    next_steps = Table(show_header=True, box=None, pad_edge=False)
    next_steps.add_column("Step", style="bold blue", no_wrap=True)
    next_steps.add_column("Action", style="white")
    next_steps.add_column("Purpose", style="cyan")
    next_steps.add_row(
        "1",
        "Edit global_config.json + per-agent config/runtime/agent-home files",
        "Prepare your multi-agent configuration.",
    )
    next_steps.add_row(
        "2",
        "ppx doctor",
        "Validate config and runtime readiness.",
    )
    next_steps.add_row(
        "3",
        (
            "ppx --config-path "
            f"{str(_agent_config_path(_INIT_DEFAULT_AGENT_NAMES[0]))} "
            "gateway run --channels local --interactive-local"
        ),
        "Try local interactive mode first.",
    )
    next_steps.add_row(
        "4",
        "ppx gateway start",
        "Start background gateway for daily usage.",
    )
    console.print(Panel(next_steps, title="[bold]Next Steps[/bold]", border_style="blue"))
    return True


def _install_step_line(step: int, total: int, message: str) -> None:
    """Render one install step line, using rich style when available."""

    try:
        from rich import print as rich_print  # type: ignore

        rich_print(f"[bold cyan]Install step {step}/{total}:[/bold cyan] {message}")
    except Exception:
        _stdout_line(f"Install step {step}/{total}: {message}")


def _install_prereq_lines() -> list[str]:
    """Build lightweight prerequisite check lines for install UX."""

    lines: list[str] = []
    cwd = Path.cwd()
    venv_dir = cwd / ".venv"
    unix_python = venv_dir / "bin" / "python"
    win_python = venv_dir / "Scripts" / "python.exe"
    if unix_python.exists() or win_python.exists():
        lines.append(f"Install prereq: virtualenv detected at {venv_dir}")
    else:
        lines.append(f"Install prereq: .venv not found under {cwd} (recommended: python3.14 -m venv .venv)")

    adk_path = shutil.which("adk")
    if adk_path:
        lines.append(f"Install prereq: adk CLI detected at {adk_path}")
    else:
        lines.append("Install prereq: adk CLI not found (recommended: pip install google-adk)")

    if importlib.util.find_spec("questionary") is None:
        lines.append("Install prereq: optional package questionary missing (interactive installer falls back to plain input)")
    else:
        lines.append("Install prereq: optional package questionary detected")
    if importlib.util.find_spec("rich") is None:
        lines.append("Install prereq: optional package rich missing (installer falls back to plain output)")
    else:
        lines.append("Install prereq: optional package rich detected")
    return lines


def _render_install_welcome_with_rich(
    *,
    mode: str,
    config_path: Path,
    runtime_config_path: Path,
) -> bool:
    """Render install welcome card when Rich + TTY are available."""
    if not sys.stdout.isatty():
        return False
    try:
        from rich.console import Console  # type: ignore
        from rich.panel import Panel  # type: ignore
        from rich.table import Table  # type: ignore
    except Exception:
        return False

    console = Console()
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("key", style="bold cyan", no_wrap=True)
    table.add_column("value", style="white")
    table.add_row("Mode", mode)
    table.add_row("Config", str(config_path))
    table.add_row("Runtime", str(runtime_config_path))
    console.print(Panel(table, title="[bold]OpenPipixia Install Wizard[/bold]", border_style="cyan"))
    return True


def _render_install_step_outcome_with_rich(*, step: int, total: int, outcome: str, message: str) -> bool:
    """Render one install step outcome line in Rich mode."""
    if not sys.stdout.isatty():
        return False
    try:
        from rich.console import Console  # type: ignore
    except Exception:
        return False
    icon = "✓"
    style = "green"
    if outcome == "warn":
        icon = "!"
        style = "yellow"
    elif outcome == "fail":
        icon = "x"
        style = "red"
    Console().print(f"[{style}]{icon}[/{style}] step {step}/{total} {message}")
    return True


def _render_install_action_plan_with_rich(*, commands: list[str], title: str = "Action Plan") -> bool:
    """Render final install next-step commands in Rich mode."""
    if not sys.stdout.isatty():
        return False
    try:
        from rich.console import Console  # type: ignore
        from rich.panel import Panel  # type: ignore
        from rich.table import Table  # type: ignore
    except Exception:
        return False
    if not commands:
        return False
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("step", style="bold blue", no_wrap=True)
    table.add_column("command", style="white")
    for idx, cmd in enumerate(commands, start=1):
        table.add_row(str(idx), cmd)
    Console().print(Panel(table, title=f"[bold]{title}[/bold]", border_style="blue"))
    return True


def _cmd_install(
    *,
    force: bool,
) -> int:
    """Run minimal install flow: init setup and doctor checks."""
    config_path = get_config_path()
    runtime_config_path = config_path.with_name("runtime.json")
    mode = "minimal"
    _render_install_welcome_with_rich(
        mode=mode,
        config_path=config_path,
        runtime_config_path=runtime_config_path,
    )

    total_steps = 2
    _install_step_line(1, total_steps, "initializing config and workspace...")
    global _INSTALL_EMBEDDED_INIT_SETUP
    _INSTALL_EMBEDDED_INIT_SETUP = True
    try:
        init_setup_code = _cmd_install_init_setup(force=force)
    finally:
        _INSTALL_EMBEDDED_INIT_SETUP = False
    if init_setup_code != 0:
        _render_install_step_outcome_with_rich(
            step=1, total=total_steps, outcome="fail", message="setup initialization failed"
        )
        return init_setup_code
    _render_install_step_outcome_with_rich(
        step=1, total=total_steps, outcome="done", message="setup initialization complete"
    )

    bootstrap_env_from_config()
    _install_step_line(2, total_steps, "running environment checks...")
    doctor_code = _cmd_doctor(output_json=False, verbose=False)
    if doctor_code != 0:
        _render_install_step_outcome_with_rich(
            step=2, total=total_steps, outcome="fail", message="doctor reported issues"
        )
        _stdout_line("Install completed with issues. Fix the items above, then rerun `ppx doctor`.")
        _render_install_action_plan_with_rich(
            commands=["ppx doctor"],
            title="Retry Plan",
        )
        return 1
    _render_install_step_outcome_with_rich(
        step=2, total=total_steps, outcome="done", message="environment checks passed"
    )

    _stdout_line("Install complete. Next: run `ppx gateway`.")
    _render_install_action_plan_with_rich(
        commands=["ppx doctor", "ppx gateway"],
    )
    return 0


def _gateway_log_dir() -> Path:
    """Return gateway runtime/log directory under ~/.openppx/log."""
    path = get_data_dir() / "log"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _gateway_pid_path() -> Path:
    """Return gateway background pid file path."""
    return _gateway_log_dir() / "gateway.pid"


def _gateway_meta_path() -> Path:
    """Return gateway background metadata file path."""
    return _gateway_log_dir() / "gateway.meta.json"


def _gateway_stdout_log_path() -> Path:
    """Return gateway background stdout log path."""
    return _gateway_log_dir() / "gateway.out.log"


def _gateway_stderr_log_path() -> Path:
    """Return gateway background stderr log path."""
    return _gateway_log_dir() / "gateway.err.log"


def _gateway_debug_log_path() -> Path:
    """Return gateway background debug log path."""
    return _gateway_log_dir() / "gateway.debug.log"


def _gateway_multi_meta_path() -> Path:
    """Return multi-agent gateway runtime metadata path."""
    return _gateway_log_dir() / "gateway.multi.meta.json"


def _read_gateway_pid() -> int | None:
    """Read gateway pid from pid file."""
    path = _gateway_pid_path()
    if not path.exists():
        return None
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    return value if value > 0 else None


def _is_pid_running(pid: int) -> bool:
    """Return whether one pid currently exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _gateway_cleanup_runtime_files(*, keep_logs: bool = True) -> None:
    """Remove gateway pid/meta files after process exits."""
    for path in (_gateway_pid_path(), _gateway_meta_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except Exception:
            continue
    if not keep_logs:
        for path in (_gateway_stdout_log_path(), _gateway_stderr_log_path(), _gateway_debug_log_path()):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except Exception:
                continue


def _gateway_cleanup_multi_runtime_files() -> None:
    """Remove multi-agent gateway runtime metadata file."""
    try:
        _gateway_multi_meta_path().unlink()
    except FileNotFoundError:
        return
    except Exception:
        return


def _write_gateway_runtime_metadata(*, pid: int, channels: str, command: list[str]) -> None:
    """Write gateway background runtime metadata file."""
    payload = {
        "pid": pid,
        "channels": channels,
        "startedAt": dt.datetime.now().astimezone().isoformat(),
        "command": command,
        "cwd": str(Path.cwd()),
        "python": sys.executable,
        "platform": sys.platform,
        "logs": {
            "stdout": str(_gateway_stdout_log_path()),
            "stderr": str(_gateway_stderr_log_path()),
            "debug": str(_gateway_debug_log_path()),
        },
    }
    _gateway_meta_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_gateway_runtime_metadata() -> dict[str, Any]:
    """Read gateway runtime metadata file with safe fallback."""
    path = _gateway_meta_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_gateway_multi_runtime_metadata() -> dict[str, Any]:
    """Read multi-agent gateway runtime metadata file with safe fallback."""
    path = _gateway_multi_meta_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_gateway_multi_runtime_metadata(
    *,
    channels_override: str,
    agent_entries: list[dict[str, Any]],
) -> None:
    """Write multi-agent gateway runtime metadata file."""
    payload = {
        "mode": "multi-agent",
        "channelsOverride": channels_override,
        "startedAt": dt.datetime.now().astimezone().isoformat(),
        "cwd": str(Path.cwd()),
        "python": sys.executable,
        "platform": sys.platform,
        "agents": agent_entries,
    }
    _gateway_multi_meta_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalize_agent_name(value: Any) -> str:
    """Normalize one agent name for filesystem/process labels."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    out: list[str] = []
    for ch in raw:
        if ch.isalnum() or ch in {"-", "_"}:
            out.append(ch)
        else:
            out.append("-")
    normalized = "".join(out).strip("-")
    return normalized


def _global_config_path() -> Path:
    """Return global multi-agent config path."""
    return get_data_dir() / "global_config.json"


def _global_enabled_agent_names() -> list[str]:
    """Read enabled agent names from ~/.openppx/global_config.json."""
    path = _global_config_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []

    agents_raw = raw.get("agents")
    entries: list[Any] = []
    if isinstance(agents_raw, list):
        entries = agents_raw
    elif isinstance(agents_raw, dict) and isinstance(agents_raw.get("list"), list):
        entries = agents_raw.get("list", [])

    enabled_names: list[str] = []
    seen: set[str] = set()
    for item in entries:
        enabled = True
        name = ""
        if isinstance(item, str):
            name = _normalize_agent_name(item)
        elif isinstance(item, dict):
            name = _normalize_agent_name(item.get("name") or item.get("id"))
            enabled = is_enabled(item.get("enabled"), default=True)
        else:
            continue
        if not name or not enabled or name in seen:
            continue
        seen.add(name)
        enabled_names.append(name)
    return enabled_names


def _agent_config_path(agent_name: str) -> Path:
    """Resolve one per-agent config path under ~/.openppx/<agent>/config.json."""
    return get_data_dir() / agent_name / "config.json"


def _resolve_target_agent_names(*, agent: str | None) -> tuple[list[str], str | None]:
    """Resolve agent selection for multi-agent aware read-style CLI commands."""
    enabled_agents = _global_enabled_agent_names()
    if not enabled_agents:
        if not agent:
            return [], None
        normalized = _normalize_agent_name(agent)
        if not normalized:
            return [], "Error: --agent is empty."
        config_path = _agent_config_path(normalized)
        if not config_path.exists():
            return [], f"Error: agent '{normalized}' config not found: {config_path}"
        return [normalized], None

    if agent:
        normalized = _normalize_agent_name(agent)
        if not normalized:
            return [], "Error: --agent is empty."
        if normalized not in enabled_agents:
            return [], (
                f"Error: agent '{normalized}' is not enabled in global_config.json. "
                f"Enabled agents: {', '.join(enabled_agents)}"
            )
        return [normalized], None
    return enabled_agents, None


def _run_agent_cli_command(*, agent_name: str, args: list[str]) -> tuple[int, str, str]:
    """Run one CLI subcommand against one agent config and capture output."""
    config_path = _agent_config_path(agent_name)
    if not config_path.exists():
        return 1, "", f"agent '{agent_name}' config not found: {config_path}"
    cmd = [
        sys.executable,
        "-m",
        "openpipixia.app.cli",
        "--config-path",
        str(config_path),
        *args,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        return 1, "", f"failed to run agent '{agent_name}' command ({exc})"
    return proc.returncode, proc.stdout, proc.stderr


def _print_agent_output_sections(results: list[tuple[str, int, str, str]]) -> int:
    """Render per-agent command output in a readable sectioned layout."""
    exit_code = 0
    for index, (agent_name, return_code, stdout_text, stderr_text) in enumerate(results):
        if index > 0:
            _stdout_line("")
        _stdout_line(f"[agent={agent_name}]")
        stdout_clean = stdout_text.strip()
        stderr_clean = stderr_text.strip()
        if stdout_clean:
            for line in stdout_clean.splitlines():
                _stdout_line(line)
        else:
            _stdout_line("(no output)")
        if stderr_clean:
            _stdout_line(f"[stderr] {stderr_clean}")
        if return_code != 0:
            _stdout_line(f"[exit_code] {return_code}")
            exit_code = 1
    return exit_code


def _agent_gateway_log_paths(agent_name: str, config_path: Path) -> tuple[Path, Path, Path]:
    """Resolve per-agent gateway stdout/stderr/debug log paths."""
    _ = agent_name
    log_dir = config_path.parent / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    return (
        log_dir / "gateway.out.log",
        log_dir / "gateway.err.log",
        log_dir / "gateway.debug.log",
    )


def _multi_agent_channel_conflict_warnings(config_paths_by_agent: dict[str, Path]) -> list[str]:
    """Build best-effort channel conflict warnings for multi-agent startup."""
    loaded: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for agent_name, path in config_paths_by_agent.items():
        try:
            loaded[agent_name] = load_config(config_path=path)
        except Exception as exc:
            warnings.append(f"agent '{agent_name}' config failed to load ({exc})")

    channel_to_agents: dict[str, list[str]] = {}
    external_channels: set[str] = {
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
    }
    signature_keys: dict[str, tuple[str, ...]] = {
        "feishu": ("appId", "verificationToken"),
        "telegram": ("token",),
        "whatsapp": ("bridgeUrl",),
        "discord": ("token", "gatewayUrl"),
        "dingtalk": ("clientId",),
        "slack": ("botToken", "appToken"),
        "qq": ("appId",),
        "weixin": ("baseUrl", "token", "stateDir"),
        "wecom": ("botId",),
    }
    signature_to_agents: dict[tuple[str, str, str], list[str]] = {}
    channel_agent_signatures: dict[str, dict[str, set[tuple[str, str]]]] = {}

    for agent_name, cfg in loaded.items():
        channels = cfg.get("channels")
        if not isinstance(channels, dict):
            continue
        for channel_name, raw_cfg in channels.items():
            if not isinstance(raw_cfg, dict):
                continue
            if not is_enabled(raw_cfg.get("enabled"), default=False):
                continue
            normalized_channel = str(channel_name).strip().lower()
            if normalized_channel not in external_channels:
                continue
            channel_to_agents.setdefault(normalized_channel, []).append(agent_name)
            for key in signature_keys.get(normalized_channel, ()):
                value = str(raw_cfg.get(key, "")).strip()
                if not value:
                    continue
                signature_to_agents.setdefault((normalized_channel, key, value), []).append(agent_name)
                channel_agent_signatures.setdefault(normalized_channel, {}).setdefault(agent_name, set()).add((key, value))

    for channel_name, agents in sorted(channel_to_agents.items()):
        unique_agents = sorted(set(agents))
        if len(unique_agents) <= 1:
            continue
        per_agent_signatures = channel_agent_signatures.get(channel_name, {})
        if channel_name in signature_keys:
            # Suppress the broad warning when every agent has at least one
            # non-empty identity signature and those signatures are distinct.
            all_agents_identified = all(per_agent_signatures.get(agent_name) for agent_name in unique_agents)
            if all_agents_identified:
                flattened: list[tuple[str, str]] = []
                for agent_name in unique_agents:
                    flattened.extend(sorted(per_agent_signatures.get(agent_name, set())))
                if len(flattened) == len(set(flattened)):
                    continue
        warnings.append(
            f"channel '{channel_name}' is enabled by multiple agents ({', '.join(unique_agents)}); "
            "verify webhook/port/account settings manually."
        )

    for (channel_name, key, _value), agents in sorted(signature_to_agents.items()):
        unique_agents = sorted(set(agents))
        if len(unique_agents) <= 1:
            continue
        warnings.append(
            f"channel '{channel_name}' key '{key}' appears duplicated across agents ({', '.join(unique_agents)}); "
            "verify webhook/port/account settings manually."
        )

    return warnings


def _multi_agent_workspace_warnings(config_paths_by_agent: dict[str, Path]) -> list[str]:
    """Warn when agent workspace still points to global default workspace path."""
    warnings: list[str] = []
    global_workspace = (get_data_dir() / "workspace").expanduser().resolve(strict=False)
    for agent_name, path in sorted(config_paths_by_agent.items()):
        try:
            cfg = load_config(config_path=path)
        except Exception as exc:
            warnings.append(f"agent '{agent_name}' workspace check skipped (config load failed: {exc})")
            continue
        agent_cfg = cfg.get("agent")
        if not isinstance(agent_cfg, dict):
            continue
        workspace_text = str(agent_cfg.get("workspace", "")).strip()
        if not workspace_text:
            continue
        workspace = Path(workspace_text).expanduser().resolve(strict=False)
        if workspace == global_workspace:
            warnings.append(
                f"agent '{agent_name}' workspace points to global default path ({workspace}); "
                "set agent.workspace to a per-agent directory."
            )
    return warnings


def _collect_running_multi_agent_entries(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Return running agent entries from one multi-agent runtime metadata payload."""
    entries = meta.get("agents")
    if not isinstance(entries, list):
        return []
    running: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        pid_raw = item.get("pid")
        try:
            pid = int(pid_raw)
        except Exception:
            continue
        if pid <= 0:
            continue
        if _is_pid_running(pid):
            running.append(item)
    return running


def _cmd_gateway_start_single(*, channels: str | None, sender_id: str, chat_id: str) -> int:
    return cli_gateway_surface.cmd_gateway_start_single(
        channels=channels,
        sender_id=sender_id,
        chat_id=chat_id,
        stdout_line=_stdout_line,
        read_gateway_pid=_read_gateway_pid,
        is_pid_running=_is_pid_running,
        gateway_cleanup_runtime_files=lambda: _gateway_cleanup_runtime_files(),
        parse_enabled_channels=parse_enabled_channels,
        get_config_path=get_config_path,
        gateway_debug_log_path=_gateway_debug_log_path,
        gateway_stdout_log_path=_gateway_stdout_log_path,
        gateway_stderr_log_path=_gateway_stderr_log_path,
        write_gateway_runtime_metadata=lambda pid, channels, command: _write_gateway_runtime_metadata(
            pid=pid,
            channels=channels,
            command=command,
        ),
        gateway_log_dir=_gateway_log_dir,
    )


def _cmd_gateway_start_multi(*, channels: str | None, sender_id: str, chat_id: str) -> int:
    return cli_gateway_surface.cmd_gateway_start_multi(
        channels=channels,
        sender_id=sender_id,
        chat_id=chat_id,
        stdout_line=_stdout_line,
        parse_enabled_channels=parse_enabled_channels,
        global_enabled_agent_names=_global_enabled_agent_names,
        agent_config_path=_agent_config_path,
        multi_agent_channel_conflict_warnings=_multi_agent_channel_conflict_warnings,
        multi_agent_workspace_warnings=_multi_agent_workspace_warnings,
        agent_gateway_log_paths=_agent_gateway_log_paths,
        write_gateway_multi_runtime_metadata=lambda channels_override, agent_entries: _write_gateway_multi_runtime_metadata(
            channels_override=channels_override,
            agent_entries=agent_entries,
        ),
    )


def _cmd_gateway_start(*, channels: str | None, sender_id: str, chat_id: str) -> int:
    """Start gateway background process (single or multi-agent)."""
    multi_meta = _read_gateway_multi_runtime_metadata()
    running_multi = _collect_running_multi_agent_entries(multi_meta)
    if running_multi:
        names = sorted({str(item.get("agent", "")) for item in running_multi if str(item.get("agent", "")).strip()})
        _stdout_line(
            "Gateway service already running in multi-agent mode: "
            + (", ".join(names) if names else f"{len(running_multi)} process(es)")
        )
        _stdout_line("Use `ppx gateway status` or `ppx gateway restart`.")
        return 0
    if multi_meta and not running_multi:
        _gateway_cleanup_multi_runtime_files()

    existing_single = _read_gateway_pid()
    if existing_single and _is_pid_running(existing_single):
        _stdout_line(f"Gateway service already running (pid={existing_single}).")
        _stdout_line("Use `ppx gateway status` or `ppx gateway restart`.")
        return 0
    if existing_single and not _is_pid_running(existing_single):
        _gateway_cleanup_runtime_files()

    if _global_enabled_agent_names():
        return _cmd_gateway_start_multi(channels=channels, sender_id=sender_id, chat_id=chat_id)
    return _cmd_gateway_start_single(channels=channels, sender_id=sender_id, chat_id=chat_id)


def _stop_gateway_pid(pid: int, *, timeout_seconds: float) -> tuple[bool, bool]:
    return cli_gateway_surface.stop_gateway_pid(
        pid=pid,
        timeout_seconds=timeout_seconds,
        is_pid_running=_is_pid_running,
    )


def _cmd_gateway_stop_single(*, timeout_seconds: float = 8.0) -> int:
    return cli_gateway_surface.cmd_gateway_stop_single(
        timeout_seconds=timeout_seconds,
        stdout_line=_stdout_line,
        read_gateway_pid=_read_gateway_pid,
        is_pid_running=_is_pid_running,
        gateway_cleanup_runtime_files=lambda: _gateway_cleanup_runtime_files(),
    )


def _cmd_gateway_stop_multi(*, timeout_seconds: float = 8.0) -> int:
    return cli_gateway_surface.cmd_gateway_stop_multi(
        timeout_seconds=timeout_seconds,
        stdout_line=_stdout_line,
        read_gateway_multi_runtime_metadata=_read_gateway_multi_runtime_metadata,
        gateway_cleanup_multi_runtime_files=_gateway_cleanup_multi_runtime_files,
        collect_running_multi_agent_entries=_collect_running_multi_agent_entries,
        write_gateway_multi_runtime_metadata=lambda channels_override, agent_entries: _write_gateway_multi_runtime_metadata(
            channels_override=channels_override,
            agent_entries=agent_entries,
        ),
        stop_gateway_pid_fn=lambda pid, timeout: _stop_gateway_pid(pid, timeout_seconds=timeout),
    )


def _cmd_gateway_stop(*, timeout_seconds: float = 8.0) -> int:
    """Stop gateway background process(es)."""
    meta = _read_gateway_multi_runtime_metadata()
    if isinstance(meta.get("agents"), list) and meta.get("agents"):
        return _cmd_gateway_stop_multi(timeout_seconds=timeout_seconds)
    return _cmd_gateway_stop_single(timeout_seconds=timeout_seconds)


def _cmd_gateway_status_single(*, output_json: bool) -> int:
    return cli_gateway_surface.cmd_gateway_status_single(
        output_json=output_json,
        stdout_line=_stdout_line,
        read_gateway_pid=_read_gateway_pid,
        is_pid_running=_is_pid_running,
        gateway_cleanup_runtime_files=lambda: _gateway_cleanup_runtime_files(),
        read_gateway_runtime_metadata=_read_gateway_runtime_metadata,
        gateway_log_dir=_gateway_log_dir,
        gateway_stdout_log_path=_gateway_stdout_log_path,
        gateway_stderr_log_path=_gateway_stderr_log_path,
        gateway_debug_log_path=_gateway_debug_log_path,
    )


def _cmd_gateway_status_multi(*, output_json: bool) -> int:
    return cli_gateway_surface.cmd_gateway_status_multi(
        output_json=output_json,
        stdout_line=_stdout_line,
        read_gateway_multi_runtime_metadata=_read_gateway_multi_runtime_metadata,
        is_pid_running=_is_pid_running,
        gateway_log_dir=_gateway_log_dir,
    )


def _cmd_gateway_status(*, output_json: bool) -> int:
    """Show gateway background status."""
    meta = _read_gateway_multi_runtime_metadata()
    if isinstance(meta.get("agents"), list) and meta.get("agents"):
        return _cmd_gateway_status_multi(output_json=output_json)
    return _cmd_gateway_status_single(output_json=output_json)


def _cmd_gateway_restart(*, channels: str | None, sender_id: str, chat_id: str) -> int:
    return cli_gateway_surface.cmd_gateway_restart(
        stop_fn=_cmd_gateway_stop,
        start_fn=lambda: _cmd_gateway_start(channels=channels, sender_id=sender_id, chat_id=chat_id),
    )


def _required_mcp_servers_from_env() -> list[str]:
    """Read strong-dependency MCP server names from environment."""
    return _parse_csv_list(os.getenv("OPENPPX_MCP_REQUIRED_SERVERS", ""))


def _is_non_blocking_mcp_issue(issue: str) -> bool:
    """Return true when MCP issue is a runtime connectivity failure only."""
    return "failed health check" in issue


async def _required_mcp_preflight(agent_tools: list[object]) -> list[str]:
    """Validate required MCP servers before gateway startup.

    Returns human-readable issue lines. Empty list means startup can continue.
    """
    required_names = _required_mcp_servers_from_env()
    if not required_names:
        return []

    issues: list[str] = []
    summaries = summarize_mcp_toolsets(agent_tools)
    configured_names = {str(item.get("name", "")) for item in summaries}
    missing = [name for name in required_names if name not in configured_names]
    if missing:
        issues.append(
            "Required MCP servers missing from configured toolsets: " + ", ".join(missing)
        )

    required_set = set(required_names)
    required_toolsets = [
        tool
        for tool in agent_tools
        if getattr(getattr(tool, "meta", None), "name", None) in required_set
    ]
    if not required_toolsets:
        return issues

    mcp_probe_policy = _load_mcp_probe_policy(
        timeout_env_name="OPENPPX_MCP_GATEWAY_TIMEOUT_SECONDS",
        timeout_default=5.0,
    )
    results = await probe_mcp_toolsets(
        required_toolsets,
        timeout_seconds=mcp_probe_policy.timeout_seconds,
        retry_attempts=mcp_probe_policy.retry_attempts,
        retry_backoff_seconds=mcp_probe_policy.retry_backoff_seconds,
    )
    for result in results:
        if str(result.get("status")) == "ok":
            continue
        name = str(result.get("name", "unknown"))
        status = str(result.get("status", "error"))
        kind = str(result.get("error_kind", "unknown")) or "unknown"
        attempts = int(result.get("attempts", 1) or 1)
        error = str(result.get("error", "")).strip()
        details = f"{status}/{kind}, attempts={attempts}"
        if error:
            details = f"{details}, error={error}"
        issues.append(f"required MCP server '{name}' failed health check ({details})")
    return issues


def _cmd_gateway(
    *,
    channels: str | None,
    sender_id: str,
    chat_id: str,
    interactive_local: bool,
) -> int:
    from .agent import root_agent
    from ..bus.queue import MessageBus
    from .gateway import Gateway

    async def _run() -> int:
        bus = MessageBus()
        names = parse_enabled_channels(channels)
        issues = validate_channel_setup(names)
        if issues:
            for item in issues:
                _stdout_line(f"[doctor] {item}")
            return 1
        if "whatsapp" in names and _whatsapp_bridge_precheck_enabled():
            bridge_issue = _check_whatsapp_bridge_ready()
            if bridge_issue:
                _stdout_line(f"[doctor] {bridge_issue}")
                return 1
        mcp_issues = await _required_mcp_preflight(list(getattr(root_agent, "tools", [])))
        if mcp_issues:
            blocking_issues = [item for item in mcp_issues if not _is_non_blocking_mcp_issue(item)]
            non_blocking_issues = [item for item in mcp_issues if _is_non_blocking_mcp_issue(item)]
            for item in non_blocking_issues:
                _stdout_line(f"[mcp] {item}; marked unavailable, gateway will continue without this MCP toolset")
            if blocking_issues:
                for item in blocking_issues:
                    _stdout_line(f"[doctor] {item}")
                return 1

        manager, local_channel = build_channel_manager(
            bus=bus,
            channel_names=names,
            local_writer=_stdout_line,
        )
        _log_mcp_startup_summary(list(getattr(root_agent, "tools", [])))
        gateway = Gateway(
            agent=root_agent,
            app_name=root_agent.name,
            bus=bus,
            channel_manager=manager,
        )
        await gateway.start()
        _stdout_line(f"gateway started with channels: {', '.join(names)}")
        if interactive_local and local_channel:
            _stdout_line("local interactive mode: type /quit or /exit to stop.")
        try:
            while True:
                if interactive_local and local_channel:
                    try:
                        line = await asyncio.to_thread(input, "> ")
                    except EOFError:
                        break
                    text = line.strip()
                    if not text:
                        continue
                    if text in {"/quit", "/exit"}:
                        break
                    await local_channel.ingest_text(text, chat_id=chat_id, sender_id=sender_id)
                    continue
                await asyncio.sleep(3600)
        finally:
            await gateway.stop()
        return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        _stdout_line(f"Error running gateway: {exc}")
        return 1


def _log_mcp_startup_summary(agent_tools: list[object]) -> None:
    """Print a compact MCP summary at gateway startup."""
    summaries = summarize_mcp_toolsets(agent_tools)
    if not summaries:
        _stdout_line("MCP toolsets: none configured")
        return
    _stdout_line(f"MCP toolsets: {len(summaries)} server(s) configured")
    for item in summaries:
        status = str(item.get("status", "unknown"))
        status_message = str(item.get("status_message", "")).strip()
        status_suffix = f", status={status}"
        if status == "unavailable" and status_message:
            status_suffix = f"{status_suffix}, reason={status_message}"
        _stdout_line(
            "MCP server "
            f"{item.get('name')}: transport={item.get('transport')}, prefix={item.get('prefix')}{status_suffix}"
        )


def _cmd_message(message: str, user_id: str, session_id: str) -> int:
    from .agent import root_agent

    _debug(
        "llm.request",
        {
            "user_id": user_id,
            "session_id": session_id,
            "message": message,
            "model": getattr(root_agent, "model", None),
            "tools": [getattr(t, "__name__", str(t)) for t in getattr(root_agent, "tools", [])],
        },
    )

    async def _run_once() -> str:
        app_name = root_agent.name
        runner, _ = create_runner(agent=root_agent, app_name=app_name)
        prompt = inject_request_time(message, received_at=dt.datetime.now().astimezone())
        from google.genai import types

        request = types.UserContent(parts=[types.Part.from_text(text=prompt)])

        final = ""
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=request):
            _debug_event(event)
            text = extract_text(getattr(event, "content", None))
            final = merge_text_stream(final, text)
        return final

    try:
        final_text = asyncio.run(_run_once())
    except Exception as exc:
        _stdout_line(f"Error running agent: {exc}")
        return 1

    if not final_text:
        _stdout_line("(no response)")
        return 0
    _stdout_line(final_text)
    return 0


def _cron_service() -> CronService:
    workspace = load_security_policy().workspace_root
    return CronService(cron_store_path(workspace))


def _cron_service_for_agent(agent_name: str) -> tuple[CronService | None, str | None]:
    return cli_runtime_ops.cron_service_for_agent(
        agent_name=agent_name,
        agent_config_path=_agent_config_path,
        load_config_fn=load_config,
    )


def _format_schedule(job) -> str:
    return format_schedule(getattr(job, "schedule", None))


def _format_ts(ms: int | None) -> str:
    return format_timestamp_ms(ms)


def _cmd_cron_list(
    *,
    include_disabled: bool,
    history: bool = False,
    history_limit: int = 20,
    agent: str | None = None,
) -> int:
    return cli_runtime_ops.cmd_cron_list(
        include_disabled=include_disabled,
        history=history,
        history_limit=history_limit,
        agent=agent,
        stdout_line=_stdout_line,
        resolve_target_agent_names=lambda value: _resolve_target_agent_names(agent=value),
        print_agent_output_sections=_print_agent_output_sections,
        cron_service_local=_cron_service,
        cron_service_for_agent_fn=_cron_service_for_agent,
    )


def _cmd_cron_add(
    *,
    name: str,
    message: str,
    every: int | None,
    cron_expr: str | None,
    tz: str | None,
    at: str | None,
    deliver: bool,
    to: str | None,
    channel: str | None,
) -> int:
    return cli_runtime_ops.cmd_cron_add(
        name=name,
        message=message,
        every=every,
        cron_expr=cron_expr,
        tz=tz,
        at=at,
        deliver=deliver,
        to=to,
        channel=channel,
        stdout_line=_stdout_line,
        cron_service_local=_cron_service,
    )


def _cmd_cron_remove(job_id: str) -> int:
    return cli_runtime_ops.cmd_cron_remove(
        job_id=job_id,
        stdout_line=_stdout_line,
        cron_service_local=_cron_service,
    )


def _cmd_cron_enable(job_id: str, *, disable: bool) -> int:
    return cli_runtime_ops.cmd_cron_enable(
        job_id=job_id,
        disable=disable,
        stdout_line=_stdout_line,
        cron_service_local=_cron_service,
    )


def _cmd_cron_run(job_id: str, *, force: bool) -> int:
    async def _run_job() -> Any:
        return await _cron_service().run_job_with_result(job_id, force=force)

    result = asyncio.run(_run_job())
    return cli_runtime_ops.cmd_cron_run(
        job_id=job_id,
        force=force,
        stdout_line=_stdout_line,
        run_job_with_result=lambda _job_id, _force: result,
    )


def _cmd_cron_status(*, agent: str | None = None) -> int:
    return cli_runtime_ops.cmd_cron_status(
        agent=agent,
        stdout_line=_stdout_line,
        resolve_target_agent_names=lambda value: _resolve_target_agent_names(agent=value),
        print_agent_output_sections=_print_agent_output_sections,
        cron_service_local=_cron_service,
        cron_service_for_agent_fn=_cron_service_for_agent,
    )


def _dispatch_cron_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    return cli_runtime_ops.dispatch_cron_command(
        args=args,
        parser=parser,
        stdout_line=_stdout_line,
        global_enabled_agent_names=_global_enabled_agent_names,
        run_agent_cli_command=lambda name, argv: _run_agent_cli_command(agent_name=name, args=argv),
        cmd_cron_list_fn=lambda include_disabled, history, history_limit, selected_agent: _cmd_cron_list(
            include_disabled=include_disabled,
            history=history,
            history_limit=history_limit,
            agent=selected_agent,
        ),
        cmd_cron_add_fn=lambda name, message, every, cron_expr, tz, at, deliver, to, channel: _cmd_cron_add(
            name=name,
            message=message,
            every=every,
            cron_expr=cron_expr,
            tz=tz,
            at=at,
            deliver=deliver,
            to=to,
            channel=channel,
        ),
        cmd_cron_remove_fn=lambda job_id: _cmd_cron_remove(job_id),
        cmd_cron_enable_fn=lambda job_id, disable: _cmd_cron_enable(job_id, disable=disable),
        cmd_cron_run_fn=lambda job_id, force: _cmd_cron_run(job_id, force=force),
        cmd_cron_status_fn=lambda selected_agent: _cmd_cron_status(agent=selected_agent),
    )


def _cmd_heartbeat_status(*, output_json: bool, agent: str | None = None) -> int:
    return cli_runtime_ops.cmd_heartbeat_status(
        output_json=output_json,
        agent=agent,
        stdout_line=_stdout_line,
        resolve_target_agent_names=lambda value: _resolve_target_agent_names(agent=value),
        run_agent_cli_command=lambda name, argv: _run_agent_cli_command(agent_name=name, args=argv),
        print_agent_output_sections=_print_agent_output_sections,
        workspace_root=lambda: load_security_policy().workspace_root,
    )


def _cmd_token_stats(
    *,
    output_json: bool,
    limit: int,
    provider: str | None,
    since: str | None,
    until: str | None,
    last_hours: int | None,
    display_utc: bool = False,
    agent: str | None = None,
) -> int:
    return cli_runtime_ops.cmd_token_stats(
        output_json=output_json,
        limit=limit,
        provider=provider,
        since=since,
        until=until,
        last_hours=last_hours,
        display_utc=display_utc,
        agent=agent,
        stdout_line=_stdout_line,
        resolve_target_agent_names=lambda value: _resolve_target_agent_names(agent=value),
        print_agent_output_sections=_print_agent_output_sections,
        agent_config_path=_agent_config_path,
        read_token_usage_stats_fn=read_token_usage_stats,
    )


def _dispatch_token_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    return cli_runtime_ops.dispatch_token_command(
        args=args,
        parser=parser,
        cmd_token_stats_fn=lambda output_json, limit, provider, since, until, last_hours, display_utc, agent: _cmd_token_stats(
            output_json=output_json,
            limit=limit,
            provider=provider,
            since=since,
            until=until,
            last_hours=last_hours,
            display_utc=display_utc,
            agent=agent,
        ),
    )


def _gateway_service_manifest_path(manager: str, service_name: str) -> Path:
    """Return user-level gateway service manifest path for one service manager."""

    if manager == "launchd":
        return Path.home() / "Library" / "LaunchAgents" / f"{service_name}.plist"
    return Path.home() / ".config" / "systemd" / "user" / f"{service_name}.service"


def _gateway_service_exec_argv(channels: str) -> tuple[str, list[str]]:
    """Return executable argv used by generated gateway service manifests."""

    ppx_bin = shutil.which("ppx")
    if ppx_bin:
        return ppx_bin, ["gateway", "run", "--channels", channels]
    return sys.executable, ["-m", "openpipixia.app.cli", "gateway", "run", "--channels", channels]


def _run_gateway_service_enable(*, manager: str, manifest_path: Path, service_name: str) -> tuple[bool, str]:
    """Enable and start the gateway service via platform-native service manager."""

    try:
        if manager == "launchd":
            subprocess.run(["launchctl", "load", "-w", str(manifest_path)], check=True)
            return True, f"launchctl loaded: {manifest_path}"
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{service_name}.service"], check=True)
        return True, f"systemd user service enabled: {service_name}.service"
    except FileNotFoundError as exc:
        return False, f"service manager command not found: {exc}"
    except subprocess.CalledProcessError as exc:
        return False, f"service manager command failed: {exc}"


def _cmd_gateway_service_install(*, force: bool, channels: str, enable: bool) -> int:
    """Install a user-level gateway service manifest for supported platforms."""

    manager = detect_service_manager()
    if manager == "unsupported":
        _stdout_line("Gateway service install is only supported on macOS (launchd) and Linux (systemd user).")
        return 1

    channels_value = ",".join(parse_enabled_channels(channels))
    service_name = gateway_service_name("openpipixia")
    manifest_path = _gateway_service_manifest_path(manager, service_name)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_path.exists() and not force:
        _stdout_line(f"Gateway service manifest already exists: {manifest_path} (use --force to overwrite)")
        return 1

    program, args = _gateway_service_exec_argv(channels_value)
    logs_dir = get_data_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = logs_dir / "gateway-service.out.log"
    stderr_log = logs_dir / "gateway-service.err.log"
    config_path = get_config_path()
    workspace = Path(
        str(load_config(config_path=config_path).get("agent", {}).get("workspace", "")).strip() or Path.cwd()
    ).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)

    if manager == "launchd":
        manifest = render_launchd_plist(
            label=service_name,
            program=program,
            args=args,
            working_directory=workspace,
            env={"OPENPPX_CHANNELS": channels_value},
            stdout_path=stdout_log,
            stderr_path=stderr_log,
        )
        enable_hint = f"launchctl load -w {manifest_path}"
        disable_hint = f"launchctl unload {manifest_path}"
    else:
        exec_start = " ".join([program, *args])
        manifest = render_systemd_unit(
            description="Openpipixia Gateway Service",
            exec_start=exec_start,
            working_directory=workspace,
            env={"OPENPPX_CHANNELS": channels_value},
        )
        enable_hint = f"systemctl --user daemon-reload && systemctl --user enable --now {service_name}.service"
        disable_hint = f"systemctl --user disable --now {service_name}.service"

    manifest_path.write_text(manifest, encoding="utf-8")
    _stdout_line("Gateway service install: this config lets OS user service manager run `ppx gateway run`.")
    _stdout_line(f"Gateway service manifest written: {manifest_path}")
    _stdout_line(f"Gateway service manager: {manager}")
    _stdout_line(f"Gateway service channels: {channels_value}")
    _stdout_line(f"Gateway service logs: stdout={stdout_log}, stderr={stderr_log}")
    _stdout_line(f"Next step (enable/start): {enable_hint}")
    _stdout_line(f"Stop/disable command: {disable_hint}")
    _stdout_line("Tip: use `ppx gateway status` for process status, `ppx gateway-service status` for manifest state.")
    if enable:
        ok, message = _run_gateway_service_enable(
            manager=manager,
            manifest_path=manifest_path,
            service_name=service_name,
        )
        if ok:
            _stdout_line(f"Gateway service enable succeeded: {message}")
            return 0
        _stdout_line(f"Gateway service enable failed: {message}")
        return 1
    return 0


def _cmd_gateway_service_status(*, output_json: bool) -> int:
    """Show gateway service manifest status for current platform."""

    manager = detect_service_manager()
    service_name = gateway_service_name("openpipixia")
    payload: dict[str, Any] = {
        "supported": manager != "unsupported",
        "manager": manager,
        "serviceName": service_name,
    }
    if manager != "unsupported":
        manifest_path = _gateway_service_manifest_path(manager, service_name)
        payload["manifestPath"] = str(manifest_path)
        payload["manifestExists"] = manifest_path.exists()

    if output_json:
        _stdout_line(json.dumps(payload, ensure_ascii=False))
    else:
        if manager == "unsupported":
            _stdout_line("Gateway service: unsupported platform")
        else:
            _stdout_line(
                "Gateway service status: "
                f"manager={manager}, "
                f"service_name={service_name}, "
                f"manifest_path={payload.get('manifestPath')}, "
                f"manifest_exists={payload.get('manifestExists')}"
            )
            if payload.get("manifestExists"):
                _stdout_line("Manifest is ready. If service is not running yet, run `ppx gateway-service install --enable`.")
            else:
                _stdout_line("Manifest not found. Run `ppx gateway-service install` first.")
            _stdout_line("Note: this checks service manifest only; use `ppx gateway status` for process runtime state.")
    return 0


def _should_require_agent_config_for_gateway(args: argparse.Namespace) -> bool:
    """Return true when command needs an explicit/available single-agent config."""
    if args.command != "gateway":
        return False
    return getattr(args, "gateway_action", None) == "run"


def _should_bootstrap_single_agent_env(args: argparse.Namespace) -> bool:
    """Return true when startup should hydrate one explicit config into process env."""

    if args.command in {"install", "create", "client-api"}:
        return False
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ppx",
        description="Lightweight skills-only agent based on Google ADK.",
    )
    parser.add_argument("-m", "--message", help="Run a single-turn request and print the response.")
    parser.add_argument("--user-id", default="local-user", help="User id for ADK session mode.")
    parser.add_argument(
        "--session-id",
        default="",
        help="Session id for ADK session mode (auto-generated if omitted).",
    )
    parser.add_argument(
        "--config-path",
        default="",
        help=argparse.SUPPRESS,
    )

    subparsers = parser.add_subparsers(dest="command", required=False)
    install_parser = subparsers.add_parser(
        "install",
        help="Run minimal install (init setup + doctor checks).",
        description=(
            "Minimal installer: initialize config/workspace and run doctor checks."
        ),
    )
    install_parser.add_argument(
        "--force",
        action="store_true",
        help="Reset config to defaults before running checks.",
    )
    create_parser = subparsers.add_parser(
        "create",
        help="Create one agent config with role-based defaults.",
    )
    create_parser.add_argument("--name", required=True, help="Agent name. Special characters become '-'.")
    create_parser.add_argument(
        "--role",
        default="assistant",
        choices=["assistant", "operator", "root"],
        help="Agent role. Defaults to assistant.",
    )
    create_parser.add_argument(
        "--workspace",
        default="",
        help="Optional workspace path. Defaults to a new directory under the system tmp directory.",
    )
    list_parser = subparsers.add_parser("list", help="List known agents and their current status.")
    list_parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit the agent list as JSON.",
    )
    enable_parser = subparsers.add_parser("enable", help="Enable one agent in global_config.json.")
    enable_parser.add_argument("name", help="Agent name.")
    disable_parser = subparsers.add_parser("disable", help="Disable one agent in global_config.json.")
    disable_parser.add_argument("name", help="Agent name.")
    delete_parser = subparsers.add_parser("delete", help="Delete one agent and remove it from global_config.json.")
    delete_parser.add_argument("name", help="Agent name.")
    skills_parser = subparsers.add_parser("skills", help="List discovered skills as JSON.")
    skills_parser.add_argument("--agent", default=None, help="Optional agent id.")
    mcps_parser = subparsers.add_parser("mcps", help="List connected MCP servers and their available APIs.")
    mcps_parser.add_argument("--agent", default=None, help="Optional agent id.")
    spawn_parser = subparsers.add_parser("spawn", help="List recent sub-agent tasks created by spawn_subagent.")
    spawn_parser.add_argument("--agent", default=None, help="Optional agent id.")
    doctor_parser = subparsers.add_parser("doctor", help="Check local runtime prerequisites.")
    doctor_parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit diagnostics as one machine-readable JSON object.",
    )
    doctor_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include detailed diagnostics in text output mode.",
    )
    doctor_parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply minimal config fixes from current environment values before checks.",
    )
    doctor_parser.add_argument(
        "--fix-dry-run",
        action="store_true",
        help="Show minimal fix plan without writing config.",
    )
    doctor_parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colorized doctor text output.",
    )

    run_parser = subparsers.add_parser("run", help="Run `adk run` for this agent.")
    run_parser.add_argument("adk_args", nargs=argparse.REMAINDER, help="Extra args passed to adk run.")
    gateway_parser = subparsers.add_parser(
        "gateway",
        help="Gateway runtime commands (run/start/stop/restart/status).",
        description=(
            "Gateway runtime command group. Use `run` for foreground runtime, or "
            "`start/stop/restart/status` for background lifecycle management."
        ),
    )
    gateway_parser.add_argument(
        "gateway_action",
        nargs="?",
        choices=["run", "start", "stop", "restart", "status"],
        help="Gateway action. Use `run` for foreground runtime.",
    )
    gateway_parser.add_argument(
        "--channels",
        default=None,
        help="Comma-separated channels. Defaults to OPENPPX_CHANNELS or 'local'.",
    )
    gateway_parser.add_argument("--sender-id", default="local-user", help="Sender id for local interactive mode.")
    gateway_parser.add_argument("--chat-id", default="terminal", help="Chat id for local interactive mode.")
    gateway_parser.add_argument(
        "--interactive-local",
        action="store_true",
        help="Enable terminal input loop when local channel is enabled.",
    )
    gateway_parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit machine-readable JSON for `gateway status`.",
    )
    client_api_parser = subparsers.add_parser(
        "client-api",
        help="Run the local HTTP + SSE client API service.",
    )
    client_api_subparsers = client_api_parser.add_subparsers(dest="client_api_command", required=True)
    client_api_serve_parser = client_api_subparsers.add_parser("serve", help="Start the local client API server.")
    client_api_serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    client_api_serve_parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765).")
    provider_parser = subparsers.add_parser("provider", help="Manage runtime LLM providers.")
    provider_subparsers = provider_parser.add_subparsers(dest="provider_command", required=True)
    provider_list_parser = provider_subparsers.add_parser("list", help="List providers available to ppx.")
    provider_list_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show extended provider metadata (runtime, oauth flags).",
    )
    provider_list_parser.add_argument(
        "--debug",
        action="store_true",
        help="Alias of --verbose for provider list output.",
    )
    provider_status_parser = provider_subparsers.add_parser("status", help="Show current provider runtime status.")
    provider_status_parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit provider status as one machine-readable JSON object.",
    )
    provider_login_parser = provider_subparsers.add_parser("login", help="Authenticate an OAuth provider.")
    provider_login_parser.add_argument(
        "provider_name",
        help="OAuth provider name, e.g. openai-codex or github-copilot.",
    )

    channels_parser = subparsers.add_parser("channels", help="Manage channel helper commands.")
    channels_subparsers = channels_parser.add_subparsers(dest="channels_command", required=True)
    channels_login_parser = channels_subparsers.add_parser("login", help="Start QR login helper for a channel.")
    channels_login_parser.add_argument(
        "channel_name",
        nargs="?",
        default="whatsapp",
        help="Channel name (default: whatsapp). Supported: whatsapp, weixin.",
    )
    channels_bridge_parser = channels_subparsers.add_parser(
        "bridge",
        help="Manage channel bridge background process.",
    )
    channels_bridge_subparsers = channels_bridge_parser.add_subparsers(dest="channels_bridge_command", required=True)
    channels_bridge_start_parser = channels_bridge_subparsers.add_parser("start", help="Start channel bridge.")
    channels_bridge_start_parser.add_argument(
        "channel_name",
        nargs="?",
        default="whatsapp",
        help="Channel name (default: whatsapp).",
    )
    channels_bridge_status_parser = channels_bridge_subparsers.add_parser("status", help="Show channel bridge status.")
    channels_bridge_status_parser.add_argument(
        "channel_name",
        nargs="?",
        default="whatsapp",
        help="Channel name (default: whatsapp).",
    )
    channels_bridge_stop_parser = channels_bridge_subparsers.add_parser("stop", help="Stop channel bridge.")
    channels_bridge_stop_parser.add_argument(
        "channel_name",
        nargs="?",
        default="whatsapp",
        help="Channel name (default: whatsapp).",
    )

    cron_parser = subparsers.add_parser("cron", help="Manage scheduled tasks.")
    cron_parser.add_argument("--agent", default=None, help="Optional agent id.")
    cron_subparsers = cron_parser.add_subparsers(dest="cron_command", required=True)

    cron_list_parser = cron_subparsers.add_parser("list", help="List scheduled cron jobs.")
    cron_list_parser.add_argument("--all", action="store_true", help="Include disabled jobs.")
    cron_list_parser.add_argument("--history", action="store_true", help="Show recent cron history instead of active jobs.")
    cron_list_parser.add_argument("--limit", type=int, default=20, help="Number of history entries to show with --history.")

    cron_add_parser = cron_subparsers.add_parser("add", help="Add a cron job.")
    cron_add_parser.add_argument("--name", required=True, help="Job name.")
    cron_add_parser.add_argument("--message", required=True, help="Task message sent to agent.")
    cron_add_parser.add_argument("--every", type=int, default=None, help="Run every N seconds.")
    cron_add_parser.add_argument("--cron", dest="cron_expr", default=None, help="Cron expression.")
    cron_add_parser.add_argument("--tz", default=None, help="IANA timezone (with --cron).")
    cron_add_parser.add_argument("--at", default=None, help="Run once at ISO datetime.")
    cron_add_parser.add_argument("--deliver", action="store_true", help="Deliver response to channel.")
    cron_add_parser.add_argument("--to", default=None, help="Recipient id for delivery.")
    cron_add_parser.add_argument("--channel", default=None, help="Target channel for delivery.")

    cron_remove_parser = cron_subparsers.add_parser("remove", help="Remove a cron job.")
    cron_remove_parser.add_argument("job_id", help="Cron job id.")

    cron_enable_parser = cron_subparsers.add_parser("enable", help="Enable or disable a cron job.")
    cron_enable_parser.add_argument("job_id", help="Cron job id.")
    cron_enable_parser.add_argument("--disable", action="store_true", help="Disable instead of enable.")

    cron_run_parser = cron_subparsers.add_parser("run", help="Run a cron job immediately.")
    cron_run_parser.add_argument("job_id", help="Cron job id.")
    cron_run_parser.add_argument("--force", action="store_true", help="Run even if job is disabled.")

    cron_subparsers.add_parser("status", help="Show cron runtime status.")

    heartbeat_parser = subparsers.add_parser("heartbeat", help="Heartbeat runtime helpers.")
    heartbeat_subparsers = heartbeat_parser.add_subparsers(dest="heartbeat_command", required=True)
    heartbeat_status_parser = heartbeat_subparsers.add_parser("status", help="Show heartbeat runtime status.")
    heartbeat_status_parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit heartbeat status snapshot as one machine-readable JSON object.",
    )
    heartbeat_status_parser.add_argument("--agent", default=None, help="Optional agent id.")
    token_parser = subparsers.add_parser("token", help="Token usage runtime helpers.")
    token_subparsers = token_parser.add_subparsers(dest="token_command", required=True)
    token_stats_parser = token_subparsers.add_parser("stats", help="Show token usage stats.")
    token_stats_parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit token stats as one machine-readable JSON object.",
    )
    token_stats_parser.add_argument("--agent", default=None, help="Optional agent id.")
    token_stats_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of recent usage records to include (default: 20).",
    )
    token_stats_parser.add_argument(
        "--provider",
        default=None,
        help="Optional provider filter, e.g. google/openai.",
    )
    token_stats_parser.add_argument(
        "--since",
        default=None,
        help=(
            "Optional inclusive start time in ISO8601. "
            "When timezone offset is omitted, local timezone is used "
            "(e.g. 2026-02-26T00:00:00)."
        ),
    )
    token_stats_parser.add_argument(
        "--until",
        default=None,
        help=(
            "Optional inclusive end time in ISO8601. "
            "When timezone offset is omitted, local timezone is used "
            "(e.g. 2026-02-26T23:59:59)."
        ),
    )
    token_stats_parser.add_argument(
        "--last-hours",
        type=int,
        default=None,
        help="Shortcut time filter for recent N hours (overrides --since/--until).",
    )
    token_stats_parser.add_argument(
        "--utc",
        dest="display_utc",
        action="store_true",
        help="Display recent record timestamps in UTC instead of local timezone.",
    )
    gateway_service_parser = subparsers.add_parser(
        "gateway-service",
        help="Manage OS user service config (launchd/systemd) that runs `ppx gateway run`.",
        description=(
            "Service-manager integration for gateway. This does not directly run foreground gateway; "
            "it installs/checks launchd/systemd user manifests used to auto-run gateway."
        ),
    )
    gateway_service_subparsers = gateway_service_parser.add_subparsers(
        dest="gateway_service_command", required=True
    )
    gateway_service_install_parser = gateway_service_subparsers.add_parser(
        "install",
        help="Install service manifest and optionally enable/start it.",
        description=(
            "Write launchd/systemd user manifest that executes `ppx gateway run --channels ...`.\n"
            "Use --enable to start service immediately via launchctl/systemctl."
        ),
    )
    gateway_service_install_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing service manifest.",
    )
    gateway_service_install_parser.add_argument(
        "--channels",
        default="local",
        help="Comma-separated channels passed to gateway runtime when service starts (default: local).",
    )
    gateway_service_install_parser.add_argument(
        "--enable",
        action="store_true",
        help="After writing manifest, run launchctl/systemctl to enable and start service now.",
    )
    gateway_service_status_parser = gateway_service_subparsers.add_parser(
        "status",
        help="Show whether service manifest exists and where it is located.",
        description="Inspect launchd/systemd user manifest state for ppx gateway service.",
    )
    gateway_service_status_parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit gateway service status as one machine-readable JSON object.",
    )

    args = parser.parse_args(argv)
    config_path = str(getattr(args, "config_path", "")).strip()
    if not config_path and _should_require_agent_config_for_gateway(args):
        default_config_path = get_config_path()
        if not default_config_path.exists():
            enabled_agents = _global_enabled_agent_names()
            if enabled_agents:
                _stdout_line(
                    "Gateway run requires agent config, but default config is missing: "
                    f"{default_config_path}"
                )
                _stdout_line(
                    "Detected enabled agents in global_config.json: "
                    + ", ".join(enabled_agents)
                )
                _stdout_line(
                    "Please pass --config-path explicitly, e.g.: "
                    f"ppx --config-path {str(_agent_config_path(enabled_agents[0]))} gateway run"
                )
                raise SystemExit(1)
    if _should_bootstrap_single_agent_env(args):
        if config_path:
            bootstrap_env_from_config(Path(config_path).expanduser())
        else:
            bootstrap_env_from_config()

    # Global `-m/--message` is single-turn mode only when no subcommand is used.
    if args.command is None and args.message:
        sid = args.session_id or uuid.uuid4().hex[:12]
        code = _cmd_message(args.message, user_id=args.user_id, session_id=sid)
    elif args.command == "cron":
        code = _dispatch_cron_command(args, parser)
    elif args.command == "heartbeat":
        if args.heartbeat_command == "status":
            code = _cmd_heartbeat_status(output_json=args.output_json, agent=args.agent)
        else:
            parser.print_help()
            code = 2
    elif args.command == "token":
        code = _dispatch_token_command(args, parser)
    elif args.command == "gateway-service":
        if args.gateway_service_command == "install":
            code = _cmd_gateway_service_install(force=args.force, channels=args.channels, enable=args.enable)
        elif args.gateway_service_command == "status":
            code = _cmd_gateway_service_status(output_json=args.output_json)
        else:
            parser.print_help()
            code = 2
    elif args.command == "client-api":
        if args.client_api_command == "serve":
            from ..runtime.client_api_service import serve_client_api

            serve_client_api(host=args.host, port=args.port)
            code = 0
        else:
            parser.print_help()
            code = 2
    elif args.command == "channels":
        code = _dispatch_channels_command(args, parser)
    elif args.command == "provider":
        code = _dispatch_provider_command(args, parser)
    else:
        def _dispatch_gateway_command() -> int:
            action = getattr(args, "gateway_action", None)
            if action is None:
                gateway_parser.print_help()
                return 0
            if action == "run":
                return _cmd_gateway(
                    channels=args.channels,
                    sender_id=args.sender_id,
                    chat_id=args.chat_id,
                    interactive_local=args.interactive_local,
                )
            if action == "start":
                return _cmd_gateway_start(channels=args.channels, sender_id=args.sender_id, chat_id=args.chat_id)
            if action == "stop":
                return _cmd_gateway_stop()
            if action == "restart":
                return _cmd_gateway_restart(channels=args.channels, sender_id=args.sender_id, chat_id=args.chat_id)
            if action == "status":
                return _cmd_gateway_status(output_json=args.output_json)
            gateway_parser.print_help()
            return 2

        handlers: dict[str, Callable[[], int]] = {
            "install": lambda: _cmd_install(force=args.force),
            "create": lambda: _cmd_create(name=args.name, role=args.role, workspace=args.workspace),
            "list": lambda: _cmd_list(output_json=args.output_json),
            "enable": lambda: _cmd_enable_disable(name=args.name, enabled=True),
            "disable": lambda: _cmd_enable_disable(name=args.name, enabled=False),
            "delete": lambda: _cmd_delete(name=args.name),
            "skills": lambda: _cmd_skills(agent=getattr(args, "agent", None)),
            "mcps": lambda: _cmd_mcps(agent=getattr(args, "agent", None)),
            "spawn": lambda: _cmd_spawn(agent=getattr(args, "agent", None)),
            "doctor": lambda: _cmd_doctor(
                output_json=args.output_json,
                verbose=args.verbose,
                fix=(args.fix or args.fix_dry_run),
                fix_dry_run=args.fix_dry_run,
                no_color=args.no_color,
            ),
            "run": lambda: _cmd_run(args.adk_args),
            "gateway": _dispatch_gateway_command,
        }
        handler = handlers.get(args.command)
        if handler is None:
            parser.print_help()
            code = 2
        else:
            code = handler()

    raise SystemExit(code)


def _debug(tag: str, payload: object, *, depth: int = 1) -> None:
    if not debug_logging_enabled():
        return
    emit_debug(tag, payload, depth=depth + 1)


def _debug_event(event: object) -> None:
    if not debug_logging_enabled():
        return
    content = getattr(event, "content", None)
    author = getattr(event, "author", "")
    partial = getattr(event, "partial", None)
    turn_complete = getattr(event, "turn_complete", None)
    finish_reason = getattr(event, "finish_reason", None)
    error_code = getattr(event, "error_code", None)
    error_message = getattr(event, "error_message", None)
    actions = getattr(event, "actions", None)
    parts_log: list[dict[str, object]] = []
    if content and getattr(content, "parts", None):
        for part in content.parts:
            row: dict[str, object] = {}
            text = getattr(part, "text", None)
            if text:
                row["text"] = text
                row["text_len"] = len(text)
            function_call = getattr(part, "function_call", None)
            if function_call:
                row["function_call"] = {
                    "name": getattr(function_call, "name", ""),
                    "args": getattr(function_call, "args", {}),
                }
            function_response = getattr(part, "function_response", None)
            if function_response:
                row["function_response"] = {
                    "name": getattr(function_response, "name", ""),
                    "response": getattr(function_response, "response", {}),
                }
            if row:
                parts_log.append(row)
    _debug(
        "llm.event",
        {
            "author": author,
            "partial": partial,
            "turn_complete": turn_complete,
            "finish_reason": finish_reason,
            "error_code": error_code,
            "error_message": error_message,
            "actions": str(actions) if actions is not None else None,
            "parts": parts_log,
        },
    )


if __name__ == "__main__":
    main()
