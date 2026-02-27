"""Minimal CLI helpers for openheron."""

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
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlparse

from google.genai import types
from loguru import logger

from ..channels.factory import build_channel_manager, parse_enabled_channels, validate_channel_setup
from ..core.config import (
    bootstrap_env_from_config,
    default_config,
    default_runtime_config,
    get_data_dir,
    get_config_path,
    get_runtime_config_path,
    load_config,
    load_runtime_config,
    save_config,
    save_runtime_config,
)
from ..core.env_utils import env_enabled
from ..core import doctor_rules, install_rules
from ..core.logging_utils import debug_logging_enabled, emit_debug
from ..core.gui_mcp import resolve_gui_mcp_from_env, resolve_gui_mcp_from_summaries
from ..core.mcp_registry import (
    ManagedMcpToolset,
    build_mcp_toolsets_from_env,
    probe_mcp_toolsets,
    summarize_mcp_toolsets,
)
from ..core.onboarding_adapters import (
    ApiKeyProviderOnboardingAdapter,
    resolve_channel_onboarding_adapter,
    resolve_provider_onboarding_adapter,
)
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
from ..core.auth_paths import (
    resolve_github_copilot_token_dir,
    resolve_openai_codex_oauth_data_dir,
)
from ..runtime.adk_utils import extract_text, merge_text_stream
from ..runtime.cron_helpers import cron_store_path, format_schedule, format_timestamp_ms
from ..runtime.cron_service import CronService
from ..runtime.cron_schedule_parser import parse_schedule_input
from ..runtime.heartbeat_status_store import read_heartbeat_status_snapshot
from ..runtime.token_usage_store import parse_time_filter_to_epoch_ms, read_token_usage_stats, token_usage_db_path
from ..runtime.gateway_service import (
    detect_service_manager,
    gateway_service_name,
    render_launchd_plist,
    render_systemd_unit,
)
from ..runtime.message_time import inject_request_time
from ..runtime.runner_factory import create_runner
from ..runtime.session_service import load_session_config
from ..core.security import load_security_policy
from ..tooling.skills_adapter import get_registry


def _stdout_line(message: str) -> None:
    """Write one plain user-facing line to stdout (without Loguru formatting)."""
    print(message)


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


def _build_doctor_channel_env_backfill_rules() -> tuple[DoctorChannelEnvBackfillRule, ...]:
    """Build doctor channel env-backfill rules from shared channel env mappings."""

    return tuple(
        DoctorChannelEnvBackfillRule(channel=channel, key=key, env_name=env_name)
        for channel, key, env_name in CHANNEL_ENV_BACKFILL_MAPPINGS
    )


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
            "OPENHERON_MCP_PROBE_RETRY_ATTEMPTS",
            2,
            minimum=1,
            maximum=5,
        ),
        retry_backoff_seconds=_read_env_float(
            "OPENHERON_MCP_PROBE_RETRY_BACKOFF_SECONDS",
            0.3,
            minimum=0.0,
            maximum=5.0,
        ),
    )


def _cmd_skills() -> int:
    registry = get_registry()
    payload = [
        {
            "name": info.name,
            "description": info.description,
            "source": info.source,
            "location": str(info.path),
        }
        for info in registry.list_skills()
    ]
    _stdout_line(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


async def _collect_connected_mcp_apis(
    toolsets: list[ManagedMcpToolset],
    *,
    timeout_seconds: float,
) -> dict[str, list[dict[str, str]]]:
    """Fetch API details for already-connected MCP toolsets."""

    def _pick_schema(raw_tool: Any, schema_name: str) -> Any:
        """Read one schema field from MCP raw tool using camel/snake conventions."""
        value = getattr(raw_tool, schema_name, None)
        if value is not None:
            return value
        if schema_name == "inputSchema":
            return getattr(raw_tool, "input_schema", None)
        if schema_name == "outputSchema":
            return getattr(raw_tool, "output_schema", None)
        return None

    def _schema_summary(schema: Any) -> str:
        """Render a concise schema summary focused on input/output fields."""
        if schema is None:
            return "(未声明)"
        if isinstance(schema, dict):
            schema_type = str(schema.get("type", ""))
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            if isinstance(properties, dict) and properties:
                required_names = set(required) if isinstance(required, list) else set()
                names: list[str] = []
                for key in properties.keys():
                    key_str = str(key)
                    if key_str in required_names:
                        names.append(f"{key_str}(required)")
                    else:
                        names.append(key_str)
                prefix = f"type={schema_type}; " if schema_type else ""
                return f"{prefix}fields={', '.join(names)}"
            if schema_type:
                return f"type={schema_type}"
        try:
            rendered = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            rendered = str(schema)
        if len(rendered) > 240:
            rendered = rendered[:237] + "..."
        return rendered

    async def _collect_one(toolset: ManagedMcpToolset) -> tuple[str, list[dict[str, str]]]:
        api_rows: list[dict[str, str]] = []
        try:
            tools = await asyncio.wait_for(
                ManagedMcpToolset.get_tools(toolset),
                timeout=max(1.0, float(timeout_seconds)),
            )
        except Exception:
            return toolset.meta.name, api_rows
        for tool in tools:
            name = str(getattr(tool, "name", "") or "").strip()
            if not name:
                continue
            raw_tool = getattr(tool, "raw_mcp_tool", None)
            description = ""
            input_summary = "(未声明)"
            output_summary = "(未声明)"
            if raw_tool is not None:
                description = (
                    str(getattr(raw_tool, "description", "") or getattr(raw_tool, "title", "") or "").strip()
                )
                input_summary = _schema_summary(_pick_schema(raw_tool, "inputSchema"))
                output_summary = _schema_summary(_pick_schema(raw_tool, "outputSchema"))
            if not description:
                description = str(getattr(tool, "description", "") or "").strip() or "(未提供)"
            api_rows.append(
                {
                    "name": name,
                    "description": description,
                    "input": input_summary,
                    "output": output_summary,
                }
            )
        api_rows.sort(key=lambda item: item.get("name", ""))
        return toolset.meta.name, api_rows

    pairs = await asyncio.gather(*[_collect_one(toolset) for toolset in toolsets])
    return {name: rows for name, rows in pairs}


def _cmd_mcps() -> int:
    """List connected MCP servers and available APIs for each server."""
    toolsets = build_mcp_toolsets_from_env(log_registered=False)
    if not toolsets:
        _stdout_line("MCP: no servers configured")
        return 0

    async def _run_mcps() -> tuple[int, list[dict[str, Any]], dict[str, list[dict[str, str]]]]:
        probe_policy = _load_mcp_probe_policy(
            timeout_env_name="OPENHERON_MCP_LIST_TIMEOUT_SECONDS",
            timeout_default=5.0,
        )
        toolsets_by_name = {toolset.meta.name: toolset for toolset in toolsets}
        try:
            results = await probe_mcp_toolsets(
                toolsets,
                timeout_seconds=probe_policy.timeout_seconds,
                retry_attempts=probe_policy.retry_attempts,
                retry_backoff_seconds=probe_policy.retry_backoff_seconds,
            )
            connected_names = [str(item.get("name", "")) for item in results if str(item.get("status")) == "ok"]
            if not connected_names:
                return 0, results, {}
            connected_toolsets = [toolsets_by_name[name] for name in connected_names if name in toolsets_by_name]
            api_names_by_server = await _collect_connected_mcp_apis(
                connected_toolsets,
                timeout_seconds=probe_policy.timeout_seconds,
            )
            return 0, results, api_names_by_server
        finally:
            # Keep MCP session cleanup on the same event loop as probe/list calls
            # to avoid "original event loop is closed" warnings from ADK.
            for toolset in toolsets:
                try:
                    await toolset.close()
                except Exception:
                    continue

    try:
        code, results, api_names_by_server = asyncio.run(_run_mcps())
    except Exception as exc:
        _stdout_line(f"MCP probe failed: {exc}")
        return 1
    if code != 0:
        return code

    connected = [item for item in results if str(item.get("status")) == "ok"]
    if not connected:
        _stdout_line("MCP: no connected servers")
        return 0

    _stdout_line(f"Connected MCP servers: {len(connected)}")
    _stdout_line("")
    for item in connected:
        server_name = str(item.get("name", "unknown"))
        transport = str(item.get("transport", "unknown"))
        api_rows = api_names_by_server.get(server_name, [])
        _stdout_line(f"- {server_name} ({transport}) | APIs: {len(api_rows)}")
        if not api_rows:
            _stdout_line("  (none)")
            _stdout_line("")
            continue
        for api in api_rows:
            api_name = str(api.get("name", "")).strip()
            api_description = str(api.get("description", "(未提供)")).strip() or "(未提供)"
            _stdout_line(f"  - {api_name}: {api_description}")
        _stdout_line("")
    return 0


def _subagent_log_path() -> Path:
    """Return JSONL log path that records accepted `spawn_subagent` tasks."""
    return load_security_policy().workspace_root / ".openheron" / "subagents.log"


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


def _cmd_spawn() -> int:
    """List sub-agent tasks created by `spawn_subagent`."""
    records = _read_subagent_records(limit=50)
    if not records:
        _stdout_line("Subagents: none")
        return 0

    _stdout_line(f"Subagents: {len(records)} recent task(s)")
    for item in records:
        task_id = str(item.get("task_id", "unknown"))
        status = str(item.get("status", "unknown"))
        channel = str(item.get("channel", "unknown"))
        chat_id = str(item.get("chat_id", "unknown"))
        created_at = str(item.get("timestamp", ""))
        prompt_preview = str(item.get("prompt_preview", "")).strip()
        _stdout_line(
            f"- {task_id} status={status} target={channel}:{chat_id} created_at={created_at}"
        )
        if prompt_preview:
            _stdout_line(f"  prompt: {prompt_preview}")
    return 0


def _check_openai_codex_oauth() -> tuple[bool, str]:
    """Check whether OpenAI Codex OAuth token is locally available."""
    try:
        from oauth_cli_kit import get_token
        from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
        from oauth_cli_kit.storage import FileTokenStorage
    except ImportError:
        return False, "oauth-cli-kit is not installed. Run: pip install oauth-cli-kit"

    try:
        storage = FileTokenStorage(
            token_filename=OPENAI_CODEX_PROVIDER.token_filename,
            data_dir=resolve_openai_codex_oauth_data_dir(),
            import_codex_cli=False,
        )
        token = get_token(storage=storage, provider=OPENAI_CODEX_PROVIDER)
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
        os.getenv("GITHUB_COPILOT_TOKEN_DIR", str(resolve_github_copilot_token_dir()))
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
            f"({detail}). Run: openheron provider login openai-codex",
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
            f"({detail}). Run: openheron provider login github-copilot",
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

    for requirement in install_rules.INSTALL_PROVIDER_SUMMARY_REQUIREMENTS:
        if requirement.env_name_resolver is None:
            continue
        env_name = requirement.env_name_resolver(active_provider) if requirement.env_name_resolver else ""
        env_value = os.getenv(env_name, "").strip() if env_name else ""
        if env_value and not str(item.get(requirement.key, "")).strip():
            item[requirement.key] = env_value
            _doctor_add_change(
                changes,
                event_sink=event_sink,
                code=requirement.doctor_env_backfill_code or "provider.env.backfilled",
                rule=requirement.doctor_env_backfill_rule,
                message=f"providers.{active_provider}.{requirement.key} <- {env_name}",
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
            "Only builtin GUI tools are enabled. Configure `tools.mcpServers.openheron_gui` to move GUI execution into MCP.",
        )
    if (not builtin_tools_enabled) and gui_mcp_configured:
        return ("mcp_only", "Builtin GUI tools are disabled. GUI execution is routed through MCP only.")
    return (
        "disabled",
        "Builtin GUI tools are disabled and no GUI MCP server is configured. GUI actions/tasks are currently unavailable.",
    )


def _normalize_agent_id_for_doctor(value: Any) -> str:
    """Normalize agent id with the same constraints as runtime routing."""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    out: list[str] = []
    for ch in raw:
        if ch.isalnum() or ch in {"_", "-"}:
            out.append(ch)
        else:
            out.append("-")
    return "".join(out).strip("-")


def _doctor_validate_multi_agent_config(config: dict[str, Any]) -> list[str]:
    """Validate multi-agent config structure and return doctor issue strings."""
    issues: list[str] = []
    agents = config.get("agents", {})
    if not isinstance(agents, dict):
        return issues
    entries = agents.get("list", [])
    if not isinstance(entries, list):
        issues.append("Invalid multi-agent config: agents.list must be an array.")
        return issues
    if not entries:
        issues.append("Invalid multi-agent config: agents.list is empty.")
        return issues

    known_ids: set[str] = set()
    default_count = 0
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            issues.append(f"Invalid multi-agent config: agents.list[{idx}] must be an object.")
            continue
        agent_id = _normalize_agent_id_for_doctor(entry.get("id"))
        if not agent_id:
            issues.append(f"Invalid multi-agent config: agents.list[{idx}].id is missing.")
            continue
        if agent_id in known_ids:
            issues.append(f"Invalid multi-agent config: duplicate agent id '{agent_id}'.")
        known_ids.add(agent_id)
        if bool(entry.get("default")):
            default_count += 1

    if default_count == 0:
        issues.append("Invalid multi-agent config: exactly one default agent is required (found 0).")
    elif default_count > 1:
        issues.append(f"Invalid multi-agent config: exactly one default agent is required (found {default_count}).")

    bindings = config.get("bindings", [])
    if not isinstance(bindings, list):
        issues.append("Invalid multi-agent config: bindings must be an array.")
        return issues
    for idx, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            issues.append(f"Invalid multi-agent config: bindings[{idx}] must be an object.")
            continue
        target_agent_id = _normalize_agent_id_for_doctor(binding.get("agentId"))
        if not target_agent_id:
            issues.append(f"Invalid multi-agent config: bindings[{idx}].agentId is missing.")
        elif target_agent_id not in known_ids:
            issues.append(
                f"Invalid multi-agent config: bindings[{idx}].agentId '{target_agent_id}' does not exist in agents.list."
            )
        match = binding.get("match", {})
        if not isinstance(match, dict) or not str(match.get("channel", "")).strip():
            issues.append(f"Invalid multi-agent config: bindings[{idx}].match.channel is required.")
    return issues


def _doctor_summarize_multi_agent_config(config: dict[str, Any]) -> dict[str, Any]:
    """Build a compact multi-agent routing summary for doctor output."""
    agents = config.get("agents", {})
    entries = agents.get("list", []) if isinstance(agents, dict) else []
    bindings = config.get("bindings", [])
    normalized_bindings = bindings if isinstance(bindings, list) else []

    by_channel: dict[str, int] = {}
    tier_counts: dict[str, int] = {"peer": 0, "account": 0, "channel": 0}
    seen_signatures: dict[tuple[str, str, str, str], str] = {}
    conflicts: list[str] = []

    for idx, item in enumerate(normalized_bindings):
        if not isinstance(item, dict):
            continue
        agent_id = _normalize_agent_id_for_doctor(item.get("agentId")) or "unknown"
        match = item.get("match", {})
        if not isinstance(match, dict):
            continue
        channel = str(match.get("channel", "")).strip().lower()
        if channel:
            by_channel[channel] = by_channel.get(channel, 0) + 1
        account = str(match.get("accountId", "")).strip().lower()
        peer = match.get("peer", {})
        peer_kind = ""
        peer_id = ""
        if isinstance(peer, dict):
            peer_kind = str(peer.get("kind", "")).strip().lower()
            peer_id = str(peer.get("id", "")).strip()

        tier = "channel"
        if peer_kind and peer_id:
            tier = "peer"
        elif account:
            tier = "account"
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

        signature = (channel, account, peer_kind, peer_id)
        previous_agent = seen_signatures.get(signature)
        if previous_agent and previous_agent != agent_id:
            conflicts.append(
                f"bindings[{idx}] duplicates route match {signature} but targets '{agent_id}' (first: '{previous_agent}')."
            )
        else:
            seen_signatures[signature] = agent_id

    return {
        "agentCount": len(entries) if isinstance(entries, list) else 0,
        "bindingCount": len(normalized_bindings),
        "byChannel": by_channel,
        "routeTierCount": tier_counts,
        "conflicts": conflicts,
    }


def _doctor_preview_multi_agent_routes(config: dict[str, Any], *, limit: int = 8) -> list[dict[str, Any]]:
    """Build route preview examples for doctor JSON output."""
    agents = config.get("agents", {})
    entries = agents.get("list", []) if isinstance(agents, dict) else []
    bindings = config.get("bindings", [])
    normalized_bindings = bindings if isinstance(bindings, list) else []

    def _default_agent_id() -> str:
        first = "main"
        for item in entries:
            if not isinstance(item, dict):
                continue
            candidate = _normalize_agent_id_for_doctor(item.get("id")) or "main"
            if first == "main":
                first = candidate
            if bool(item.get("default")):
                return candidate
        return first

    preview: list[dict[str, Any]] = []
    for idx, item in enumerate(normalized_bindings):
        if len(preview) >= limit:
            break
        if not isinstance(item, dict):
            continue
        match = item.get("match", {})
        if not isinstance(match, dict):
            continue
        channel = str(match.get("channel", "")).strip().lower()
        if not channel:
            continue
        agent_id = _normalize_agent_id_for_doctor(item.get("agentId")) or "main"
        account_id = str(match.get("accountId", "")).strip().lower() or "default"
        peer = match.get("peer", {})
        peer_kind = "direct"
        peer_id = "sample-peer"
        tier = "channel"
        if isinstance(peer, dict):
            raw_peer_kind = str(peer.get("kind", "")).strip().lower()
            raw_peer_id = str(peer.get("id", "")).strip()
            if raw_peer_kind and raw_peer_id:
                peer_kind = "direct" if raw_peer_kind in {"dm", "direct"} else raw_peer_kind
                peer_id = raw_peer_id
                tier = "peer"
            elif str(match.get("accountId", "")).strip():
                tier = "account"
        elif str(match.get("accountId", "")).strip():
            tier = "account"

        session_id = f"agent:{agent_id}:{channel}:{account_id}:{peer_kind}:{peer_id}"
        preview.append(
            {
                "type": "binding",
                "bindingIndex": idx,
                "tier": tier,
                "agentId": agent_id,
                "channel": channel,
                "accountId": account_id,
                "peerKind": peer_kind,
                "peerId": peer_id,
                "sessionIdExample": session_id,
            }
        )

    if len(preview) < limit:
        default_agent = _default_agent_id()
        preview.append(
            {
                "type": "default",
                "agentId": default_agent,
                "channel": "local",
                "accountId": "default",
                "peerKind": "direct",
                "peerId": "sample-peer",
                "sessionIdExample": f"agent:{default_agent}:local:default:direct:sample-peer",
            }
        )
    return preview[:limit]


def _cmd_doctor(
    *,
    output_json: bool = False,
    verbose: bool = False,
    fix: bool = False,
    fix_dry_run: bool = False,
) -> int:
    """Run runtime diagnostics and return a process exit code.

    When `output_json` is true, emits one machine-readable JSON payload.
    """
    config_path = get_config_path()
    config_payload = load_config(config_path=config_path)
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
    provider_name = normalize_provider_name(os.getenv("OPENHERON_PROVIDER"))
    provider_model = normalize_model_name(provider_name, os.getenv("OPENHERON_MODEL"))
    provider_enabled = env_enabled("OPENHERON_PROVIDER_ENABLED", default=True)
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
                f"in ~/.openheron/config.json or export {provider_key_env}."
            )
        oauth_issue, provider_oauth = _provider_oauth_health(provider_name)
        if oauth_issue:
            issues.append(oauth_issue)

    registry = get_registry()
    skills_count = len(registry.list_skills())
    session_cfg = load_session_config()
    configured_channels = parse_enabled_channels(None)
    channel_issues = validate_channel_setup(configured_channels)
    issues.extend(channel_issues)
    multi_agent_issues = _doctor_validate_multi_agent_config(config_payload)
    multi_agent_summary = _doctor_summarize_multi_agent_config(config_payload)
    multi_agent_preview = _doctor_preview_multi_agent_routes(config_payload)
    issues.extend(multi_agent_issues)
    if "whatsapp" in configured_channels and _whatsapp_bridge_precheck_enabled():
        bridge_issue = _check_whatsapp_bridge_ready()
        if bridge_issue:
            issues.append(bridge_issue)
    heartbeat_snapshot = read_heartbeat_status_snapshot(registry.workspace)
    install_prereqs = _install_prereq_lines()
    web_enabled = env_enabled("OPENHERON_WEB_ENABLED", default=True)
    web_search_enabled = env_enabled("OPENHERON_WEB_SEARCH_ENABLED", default=True)
    web_search_provider = os.getenv("OPENHERON_WEB_SEARCH_PROVIDER", "brave").strip().lower() or "brave"
    web_search_key_configured = bool(os.getenv("BRAVE_API_KEY", "").strip())
    security_policy = load_security_policy()
    mcp_toolsets = build_mcp_toolsets_from_env(log_registered=False)
    mcp_summaries = summarize_mcp_toolsets(mcp_toolsets)
    gui_builtin_tools_enabled = env_enabled("OPENHERON_GUI_BUILTIN_TOOLS_ENABLED", default=True)
    gui_mcp = resolve_gui_mcp_from_env() or resolve_gui_mcp_from_summaries(mcp_summaries)
    gui_task_tool = gui_mcp.task_tool_name if gui_mcp else None
    gui_action_tool = gui_mcp.action_tool_name if gui_mcp else None
    gui_mode, gui_hint = _gui_execution_path_hint(
        builtin_tools_enabled=gui_builtin_tools_enabled,
        gui_task_tool=gui_task_tool,
        gui_action_tool=gui_action_tool,
    )
    mcp_probe_policy = _load_mcp_probe_policy(
        timeout_env_name="OPENHERON_MCP_DOCTOR_TIMEOUT_SECONDS",
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
        "session": {"db_url": session_cfg.db_url},
        "channels": {"configured": configured_channels},
        "multiAgent": {
            "issues": multi_agent_issues,
            "summary": multi_agent_summary,
            "routePreview": multi_agent_preview,
            "agent_count": len(config_payload.get("agents", {}).get("list", []))
            if isinstance(config_payload.get("agents", {}).get("list", []), list)
            else 0,
            "binding_count": len(config_payload.get("bindings", []))
            if isinstance(config_payload.get("bindings", []), list)
            else 0,
        },
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

    if output_json:
        _stdout_line(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1

    logger.debug(f"Config file: {config_path}" + (" (found)" if config_path.exists() else " (not found)"))
    logger.debug(f"Workspace: {registry.workspace}")
    logger.debug(f"Detected skills: {skills_count}")
    logger.debug(f"Provider: {provider_name} (enabled={provider_enabled}, model={provider_model})")
    logger.debug(
        "Provider OAuth: "
        f"required={provider_oauth.get('required')}, "
        f"authenticated={provider_oauth.get('authenticated')}, "
        f"message={provider_oauth.get('message')}"
    )
    logger.debug(f"Session storage: sqlite ({session_cfg.db_url})")
    logger.debug(f"Configured channels: {', '.join(configured_channels) if configured_channels else '(none)'}")
    logger.debug(
        "Heartbeat status snapshot: "
        f"{'available' if heartbeat_snapshot is not None else 'missing'}"
    )
    logger.debug(
        "Web search: "
        f"enabled={web_enabled and web_search_enabled}, "
        f"provider={web_search_provider}, "
        f"api_key={'configured' if web_search_key_configured else 'missing'}"
    )
    logger.debug(
        "Security: "
        f"restrict_to_workspace={security_policy.restrict_to_workspace}, "
        f"allow_exec={security_policy.allow_exec}, "
        f"allow_network={security_policy.allow_network}, "
        f"exec_allowlist={list(security_policy.exec_allowlist)}"
    )
    logger.debug(
        "GUI execution: "
        f"builtin_tools_enabled={gui_builtin_tools_enabled}, "
        f"mode={gui_mode}, "
        f"hint={gui_hint}"
    )
    if not mcp_summaries:
        logger.debug("MCP: no servers configured")
    else:
        logger.debug(f"MCP: configured servers={len(mcp_summaries)}")
        for item in mcp_summaries:
            logger.debug(
                "MCP: "
                f"name={item.get('name')}, "
                f"transport={item.get('transport')}, "
                f"prefix={item.get('prefix')}"
            )
        for result in mcp_probe_results:
            logger.debug(
                "MCP health: "
                f"name={result.get('name')}, "
                f"status={result.get('status')}, "
                f"tools={result.get('tool_count')}, "
                f"elapsed_ms={result.get('elapsed_ms')}, "
                f"attempts={result.get('attempts')}, "
                f"error_kind={result.get('error_kind')}, "
                f"error={result.get('error')}"
            )
    if verbose:
        _stdout_line(
            "Multi-agent summary: "
            f"agents={multi_agent_summary.get('agentCount', 0)}, "
            f"bindings={multi_agent_summary.get('bindingCount', 0)}, "
            f"channels={len(multi_agent_summary.get('byChannel', {}))}, "
            f"conflicts={len(multi_agent_summary.get('conflicts', []))}"
        )
        for item in multi_agent_preview[:5]:
            _stdout_line(
                "Multi-agent preview: "
                f"type={item.get('type')}, "
                f"tier={item.get('tier', '-')}, "
                f"agent={item.get('agentId')}, "
                f"session={item.get('sessionIdExample')}"
            )
        for item in multi_agent_summary.get("conflicts", [])[:10]:
            _stdout_line(f"Multi-agent conflict: {item}")
        _stdout_line("Doctor details:")
        _stdout_line(json.dumps(report, ensure_ascii=False, indent=2))

    for line in install_prereqs:
        _stdout_line(_doctor_install_prereq_line(line))

    if heartbeat_snapshot is None:
        _stdout_line("Heartbeat: snapshot=missing")
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
    conflict_count = len(multi_agent_summary.get("conflicts", []))
    if conflict_count > 0:
        _stdout_line(
            "Multi-agent routing conflicts detected: "
            f"{conflict_count}. Review doctor --json fields "
            "`multiAgent.summary.conflicts` and `multiAgent.routePreview`."
        )

    if issues:
        _stdout_line("Issues:")
        for item in issues:
            _stdout_line(f"- {item}")
        return 1

    _stdout_line("Environment looks good.")
    return 0


def _cmd_routes_lint(*, output_json: bool = False, limit: int = 8) -> int:
    """Lint multi-agent route config and report structural/routing conflicts."""
    config_path = get_config_path()
    config_payload = load_config(config_path=config_path)
    issues = _doctor_validate_multi_agent_config(config_payload)
    summary = _doctor_summarize_multi_agent_config(config_payload)
    preview = _doctor_preview_multi_agent_routes(config_payload, limit=max(1, min(limit, 50)))
    conflicts = summary.get("conflicts", [])
    suggestions: list[str] = []
    if conflicts:
        suggestions.append(
            "For each duplicate signature in conflicts, keep exactly one target agent for that channel/accountId/peer."
        )
        suggestions.append("If you need special routing for one user/chat, add a higher-priority peer rule instead.")
    for item in issues:
        text = str(item).lower()
        if "default agent is required" in text:
            suggestions.append("Set exactly one `agents.list[*].default=true`.")
        elif "agentid" in text and "does not exist" in text:
            suggestions.append("Fix `bindings[*].agentId` to an existing id in `agents.list[*].id`.")
        elif "match.channel is required" in text:
            suggestions.append("Set `bindings[*].match.channel` for every binding entry.")
        elif "duplicate agent id" in text:
            suggestions.append("Ensure each `agents.list[*].id` is unique.")
    # Keep output compact and deterministic.
    suggestions = list(dict.fromkeys(suggestions))

    report: dict[str, Any] = {
        "ok": not issues and not conflicts,
        "config": {"path": str(config_path), "exists": config_path.exists()},
        "issues": issues,
        "summary": summary,
        "routePreview": preview,
        "suggestions": suggestions,
    }

    if output_json:
        _stdout_line(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1

    _stdout_line(
        "Routes lint: "
        f"ok={report['ok']}, "
        f"agents={summary.get('agentCount', 0)}, "
        f"bindings={summary.get('bindingCount', 0)}, "
        f"conflicts={len(conflicts)}"
    )
    if issues:
        _stdout_line("Routing issues:")
        for item in issues:
            _stdout_line(f"- {item}")
    if conflicts:
        _stdout_line("Routing conflicts:")
        for item in conflicts[:20]:
            _stdout_line(f"- {item}")
    if preview:
        _stdout_line("Route preview:")
        for item in preview[: min(5, len(preview))]:
            _stdout_line(
                "- "
                f"type={item.get('type')}, "
                f"tier={item.get('tier', '-')}, "
                f"agent={item.get('agentId')}, "
                f"session={item.get('sessionIdExample')}"
            )
    if suggestions:
        _stdout_line("Suggested actions:")
        for item in suggestions:
            _stdout_line(f"- {item}")
    return 0 if report["ok"] else 1


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
        from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
        from oauth_cli_kit.storage import FileTokenStorage
    except ImportError as exc:
        raise RuntimeError("oauth-cli-kit is not installed. Run: pip install oauth-cli-kit") from exc

    storage = FileTokenStorage(
        token_filename=OPENAI_CODEX_PROVIDER.token_filename,
        data_dir=resolve_openai_codex_oauth_data_dir(),
        import_codex_cli=False,
    )
    token = None
    try:
        token = get_token(storage=storage, provider=OPENAI_CODEX_PROVIDER)
    except Exception:
        token = None

    has_access = bool(token and getattr(token, "access", ""))
    has_account_id = bool(token and getattr(token, "account_id", ""))
    if not (has_access and has_account_id):
        _stdout_line("Starting interactive OAuth login for OpenAI Codex...")
        token = login_oauth_interactive(
            print_fn=lambda s: _stdout_line(str(s)),
            prompt_fn=lambda s: input(str(s)),
            provider=OPENAI_CODEX_PROVIDER,
            storage=storage,
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
    token_dir = resolve_github_copilot_token_dir()
    token_dir.mkdir(parents=True, exist_ok=True)

    async def _trigger() -> None:
        from litellm import acompletion

        await acompletion(
            model="github_copilot/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )

    previous = os.environ.get("GITHUB_COPILOT_TOKEN_DIR")
    os.environ["GITHUB_COPILOT_TOKEN_DIR"] = str(token_dir)
    try:
        asyncio.run(_trigger())
    finally:
        if previous is None:
            os.environ.pop("GITHUB_COPILOT_TOKEN_DIR", None)
        else:
            os.environ["GITHUB_COPILOT_TOKEN_DIR"] = previous
    _stdout_line("GitHub Copilot OAuth authenticated.")


def _cmd_provider_list() -> int:
    """List providers known by the runtime."""
    _stdout_line("Providers:")
    for name in provider_names():
        spec = find_provider_spec(name)
        if not spec:
            _stdout_line(f"- {name}")
            continue
        oauth_flag = ", oauth=true" if spec.is_oauth else ""
        _stdout_line(
            f"- {name}: runtime={spec.runtime}, default_model={spec.default_model}{oauth_flag}"
        )
    return 0


def _cmd_provider_status(*, output_json: bool = False) -> int:
    """Show runtime status for currently selected provider."""
    provider_name = normalize_provider_name(os.getenv("OPENHERON_PROVIDER"))
    provider_model = normalize_model_name(provider_name, os.getenv("OPENHERON_MODEL"))
    provider_enabled = env_enabled("OPENHERON_PROVIDER_ENABLED", default=True)
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
    handlers: dict[str, Callable[[], int]] = {
        "list": _cmd_provider_list,
        "status": lambda: _cmd_provider_status(output_json=args.output_json),
        "login": lambda: _cmd_provider_login(args.provider_name),
    }
    handler = handlers.get(args.provider_command)
    if handler is None:
        parser.error("provider command is required")
    return handler()


def _dispatch_routes_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch routes subcommands from parsed argparse namespace."""
    handlers: dict[str, Callable[[], int]] = {
        "lint": lambda: _cmd_routes_lint(output_json=args.output_json, limit=args.limit),
    }
    handler = handlers.get(args.routes_command)
    if handler is None:
        parser.error("routes command is required")
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
    override = os.getenv("OPENHERON_WHATSAPP_BRIDGE_SOURCE", "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())

    package_bridge = Path(__file__).resolve().parent / "bridge"
    monorepo_bridge = Path(__file__).resolve().parents[2] / "openheron_root" / "openheron" / "bridge"
    candidates.extend([package_bridge, monorepo_bridge])

    for candidate in candidates:
        if (candidate / "package.json").exists():
            return candidate
    raise RuntimeError(
        "WhatsApp bridge source not found. "
        "Set OPENHERON_WHATSAPP_BRIDGE_SOURCE or include openheron/bridge resources."
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


def _cmd_channels_login(*, channel_name: str) -> int:
    """Start channel login helper (currently WhatsApp QR bridge only)."""
    target = channel_name.strip().lower()
    if target != "whatsapp":
        _stdout_line(f"Unsupported channel for login: {channel_name}. Supported: whatsapp")
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
    return env_enabled("OPENHERON_WHATSAPP_BRIDGE_PRECHECK", default=True)


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
            "Run: openheron channels bridge start"
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


@dataclass(frozen=True)
class InstallInitResult:
    """Resolved install init side effects for config/runtime/workspace paths."""

    config_path: Path
    runtime_config_path: Path
    workspace: Path
    config_state: str
    runtime_state: str


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
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
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
    print("5. Start gateway: openheron gateway")
    print("6. Dry run: openheron doctor")
    return 0


InstallChannelPromptRule = install_rules.InstallChannelPromptRule
InstallProviderSummaryRequirement = install_rules.InstallProviderSummaryRequirement
InstallChannelSummaryRequirement = install_rules.InstallChannelSummaryRequirement
INSTALL_CHANNEL_PROMPT_RULES = install_rules.INSTALL_CHANNEL_PROMPT_RULES
INSTALL_CHANNEL_SUMMARY_REQUIREMENTS = install_rules.build_install_channel_summary_requirements()
INSTALL_PROVIDER_SUMMARY_REQUIREMENTS = install_rules.INSTALL_PROVIDER_SUMMARY_REQUIREMENTS


def _build_install_channel_summary_requirements() -> tuple[InstallChannelSummaryRequirement, ...]:
    """Compatibility wrapper: delegate summary requirement build to install rules module."""

    return install_rules.build_install_channel_summary_requirements()


def _install_summary_provider_missing(*, selected_provider: str, provider_cfg: dict[str, Any]) -> list[str]:
    """Compatibility wrapper: delegate provider missing checks to install rules module."""

    return install_rules.install_summary_provider_missing(
        selected_provider=selected_provider,
        provider_cfg=provider_cfg,
        requirements=INSTALL_PROVIDER_SUMMARY_REQUIREMENTS,
    )


def _install_summary_provider_fix_hints(*, selected_provider: str, config_path: Path) -> dict[str, str]:
    """Compatibility wrapper: delegate provider hint rendering to install rules module."""

    return install_rules.install_summary_provider_fix_hints(
        selected_provider=selected_provider,
        config_path=config_path,
        requirements=INSTALL_PROVIDER_SUMMARY_REQUIREMENTS,
    )


def _install_channel_value_missing(
    *,
    cfg: dict[str, Any],
    key: str,
    presence: Literal["truthy_bool", "non_empty_strip", "non_empty_raw"],
) -> bool:
    """Compatibility wrapper: delegate channel missing predicate to install rules module."""

    return install_rules.install_channel_value_missing(cfg=cfg, key=key, presence=presence)


def _install_summary_channel_missing(channels_cfg: dict[str, Any]) -> list[str]:
    """Compatibility wrapper: delegate channel missing checks to install rules module."""

    return install_rules.install_summary_channel_missing(
        channels_cfg=channels_cfg,
        requirements=INSTALL_CHANNEL_SUMMARY_REQUIREMENTS,
    )


def _install_summary_channel_fix_hints(config_path: Path) -> dict[str, str]:
    """Compatibility wrapper: delegate channel hint rendering to install rules module."""

    return install_rules.install_summary_channel_fix_hints(
        config_path=config_path,
        requirements=INSTALL_CHANNEL_SUMMARY_REQUIREMENTS,
    )


def _install_summary_missing(
    *,
    selected_provider: str,
    provider_cfg: dict[str, Any],
    channels_cfg: dict[str, Any],
) -> list[str]:
    """Compatibility wrapper: delegate missing aggregation to install rules module."""

    return install_rules.install_summary_missing(
        selected_provider=selected_provider,
        provider_cfg=provider_cfg,
        channels_cfg=channels_cfg,
        provider_requirements=INSTALL_PROVIDER_SUMMARY_REQUIREMENTS,
        channel_requirements=INSTALL_CHANNEL_SUMMARY_REQUIREMENTS,
    )


def _install_summary_fix_hints(*, missing: list[str], selected_provider: str, config_path: Path) -> list[str]:
    """Compatibility wrapper: delegate fix-hint rendering to install rules module."""

    return install_rules.install_summary_fix_hints(
        missing=missing,
        selected_provider=selected_provider,
        config_path=config_path,
        provider_requirements=INSTALL_PROVIDER_SUMMARY_REQUIREMENTS,
        channel_requirements=INSTALL_CHANNEL_SUMMARY_REQUIREMENTS,
    )


def _apply_install_channel_prompt_rules(
    *,
    channels_cfg: dict[str, Any],
    enabled_channels: list[str],
    input_fn: Callable[[str], str],
    secret_input_fn: Callable[[str], str] | None = None,
) -> None:
    """Compatibility wrapper: delegate channel credential prompts to install rules module."""

    install_rules.apply_install_channel_prompt_rules(
        channels_cfg=channels_cfg,
        enabled_channels=enabled_channels,
        input_fn=input_fn,
        secret_input_fn=secret_input_fn,
    )


def _active_provider_name(providers_cfg: dict[str, Any]) -> str:
    """Resolve currently enabled provider from config payload."""
    for name in provider_names():
        item = providers_cfg.get(name, {})
        if isinstance(item, dict) and bool(item.get("enabled")):
            return name
    return DEFAULT_PROVIDER


def _provider_choice_label(*, name: str, cfg: dict[str, Any], is_current: bool) -> str:
    """Build one provider select label with minimal noise."""
    if is_current:
        return f"{name} (current)"
    return name


def _channel_missing_count(channel_name: str, channel_cfg: dict[str, Any]) -> int:
    """Return required missing field count for one channel config snapshot."""
    count = 0
    for requirement in INSTALL_CHANNEL_SUMMARY_REQUIREMENTS:
        if requirement.channel != channel_name:
            continue
        if install_rules.install_channel_value_missing(
            cfg=channel_cfg,
            key=requirement.key,
            presence=requirement.presence,
        ):
            count += 1
    return count


def _channel_choice_label(*, name: str, cfg: dict[str, Any], enabled_now: bool) -> str:
    """Build one channel multi-select label with readiness signal."""
    enabled_tag = "enabled" if enabled_now else "disabled"
    if name == "local":
        readiness = "ready"
    else:
        missing = _channel_missing_count(name, cfg)
        readiness = "ready" if missing == 0 else f"missing:{missing}"
    return f"{name} [{enabled_tag}] [{readiness}]"


def _install_snapshot_state(config: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """Collect provider/channels/missing snapshot for install UI rendering."""
    providers_cfg = config.get("providers", {})
    provider_cfg_dict = providers_cfg if isinstance(providers_cfg, dict) else {}
    selected_provider = _active_provider_name(provider_cfg_dict)

    channels_cfg = config.get("channels", {})
    channels_cfg_dict = channels_cfg if isinstance(channels_cfg, dict) else {}
    enabled_channels: list[str] = []
    for name, item in channels_cfg_dict.items():
        if isinstance(item, dict) and bool(item.get("enabled")):
            enabled_channels.append(str(name))

    missing = install_rules.install_summary_missing(
        selected_provider=selected_provider,
        provider_cfg=provider_cfg_dict,
        channels_cfg=channels_cfg_dict,
        provider_requirements=INSTALL_PROVIDER_SUMMARY_REQUIREMENTS,
        channel_requirements=INSTALL_CHANNEL_SUMMARY_REQUIREMENTS,
    )
    return selected_provider, enabled_channels, missing


def _render_install_interactive_snapshot_with_rich(*, title: str, config: dict[str, Any]) -> bool:
    """Render one compact two-panel install snapshot in interactive setup."""
    if not sys.stdout.isatty():
        return False
    try:
        from rich.columns import Columns  # type: ignore
        from rich.console import Console  # type: ignore
        from rich.panel import Panel  # type: ignore
        from rich.table import Table  # type: ignore
    except Exception:
        return False

    selected_provider, enabled_channels, missing = _install_snapshot_state(config)

    summary_table = Table(show_header=False, box=None, pad_edge=False)
    summary_table.add_column("key", style="bold cyan", no_wrap=True)
    summary_table.add_column("value", style="white")
    summary_table.add_row("Provider", selected_provider)
    summary_table.add_row("Channels", ", ".join(enabled_channels) if enabled_channels else "(none)")
    summary_table.add_row("Missing", str(len(missing)))
    summary_panel = Panel(summary_table, title="[bold]Current Selection[/bold]", border_style="cyan")

    checklist_table = Table(show_header=False, box=None, pad_edge=False)
    checklist_table.add_column("item", style="yellow")
    if missing:
        for item in missing[:8]:
            checklist_table.add_row(f"□ {item}")
        if len(missing) > 8:
            checklist_table.add_row(f"... {len(missing) - 8} more")
    else:
        checklist_table.add_row("[green]✓ Required fields look good[/green]")
    checklist_panel = Panel(checklist_table, title="[bold]Checklist[/bold]", border_style="yellow")

    console = Console()
    console.print(Panel(f"[bold]{title}[/bold]", border_style="blue"))
    console.print(Columns([summary_panel, checklist_panel], equal=True, expand=True))
    return True


def _review_install_missing_fields(
    *,
    providers_cfg: dict[str, Any],
    channels_cfg: dict[str, Any],
    input_fn: Callable[[str], str],
    secret_input_fn: Callable[[str], str] | None = None,
) -> None:
    """Offer one guided pass to fill missing required fields for enabled items."""
    selected_provider = _active_provider_name(providers_cfg)
    provider_cfg = providers_cfg if isinstance(providers_cfg, dict) else {}
    channel_cfg = channels_cfg if isinstance(channels_cfg, dict) else {}
    max_rounds = 2
    rounds = 0
    while rounds < max_rounds:
        missing = install_rules.install_summary_missing(
            selected_provider=selected_provider,
            provider_cfg=provider_cfg,
            channels_cfg=channel_cfg,
            provider_requirements=INSTALL_PROVIDER_SUMMARY_REQUIREMENTS,
            channel_requirements=INSTALL_CHANNEL_SUMMARY_REQUIREMENTS,
        )
        if not missing:
            return

        _stdout_line("Install setup: still missing required fields for enabled provider/channels:")
        for item in missing:
            _stdout_line(f"- {item}")
        answer = input_fn("Install setup: fill these missing fields now? (Y/n)> ").strip().lower()
        if answer in {"n", "no"}:
            return

        if f"{selected_provider}.apiKey" in missing:
            provider_adapter = resolve_provider_onboarding_adapter(selected_provider)
            key_env = provider_api_key_env(selected_provider)
            provider_spec = find_provider_spec(selected_provider)
            if provider_adapter is None and key_env and not (provider_spec and provider_spec.is_oauth):
                provider_adapter = ApiKeyProviderOnboardingAdapter(
                    provider_name=selected_provider,
                    prompt=f"API key for {selected_provider} (required for enabled provider)> ",
                )
            if provider_adapter is not None and isinstance(provider_cfg.get(selected_provider), dict):
                provider_adapter.collect_credentials(
                    provider_cfg=provider_cfg[selected_provider],
                    input_fn=input_fn,
                    secret_input_fn=secret_input_fn,
                )

        channels_to_review: list[str] = []
        for item in missing:
            if not item.startswith("channels."):
                continue
            parts = item.split(".")
            if len(parts) < 3:
                continue
            channel_name = parts[1]
            if channel_name not in channels_to_review:
                channels_to_review.append(channel_name)
        if channels_to_review:
            install_rules.apply_install_channel_prompt_rules(
                channels_cfg=channel_cfg,
                enabled_channels=channels_to_review,
                input_fn=input_fn,
                secret_input_fn=secret_input_fn,
            )
        rounds += 1


def _run_install_interactive_setup(
    *,
    config_path: Path,
    input_fn: Callable[[str], str],
    select_fn: Callable[[str, list[str], str], str] | None = None,
    secret_input_fn: Callable[[str], str] | None = None,
    multi_select_fn: Callable[[str, list[str], list[str]], list[str]] | None = None,
    review_missing: bool = False,
) -> None:
    """Collect minimal interactive install choices and persist config changes."""

    config = load_config(config_path=config_path)
    _render_install_interactive_snapshot_with_rich(title="Setup: Provider", config=config)
    providers_cfg = config.get("providers", {})
    available = [name for name in provider_names() if name in providers_cfg]
    if not available:
        return
    enabled_now = next((name for name in available if providers_cfg.get(name, {}).get("enabled")), available[0])
    labels = [
        _provider_choice_label(
            name=name,
            cfg=providers_cfg.get(name, {}) if isinstance(providers_cfg.get(name, {}), dict) else {},
            is_current=(name == enabled_now),
        )
        for name in available
    ]
    label_to_provider = {label: name for label, name in zip(labels, available)}

    attempts = 0
    while attempts < 3:
        if select_fn is not None:
            raw_provider = select_fn(
                "Choose provider (status shown as current/enabled/credential)",
                [*labels, "skip"],
                next((label for label in labels if label_to_provider[label] == enabled_now), labels[0]),
            ).strip()
        else:
            _stdout_line(
                f"Install setup: choose provider {available} (current: {enabled_now}, Enter keeps current, 'skip' to skip)."
            )
            raw_provider = input_fn("Provider> ").strip()
        if not raw_provider or raw_provider.lower() == "skip":
            break
        selected = label_to_provider.get(raw_provider)
        if not selected:
            selected = canonical_provider_name(raw_provider)
        if selected in available:
            for name in available:
                providers_cfg[name]["enabled"] = name == selected
            enabled_now = selected
            selected_cfg = providers_cfg.get(selected, {})
            if not isinstance(selected_cfg, dict):
                selected_cfg = {}
            _stdout_line(
                "Install setup: provider selected -> "
                f"{_provider_choice_label(name=selected, cfg=selected_cfg, is_current=True)}"
            )
            break
        attempts += 1
        _stdout_line(f"Install setup: unknown provider '{raw_provider}', try again or input 'skip'.")

    key_env = provider_api_key_env(enabled_now)
    provider_spec = find_provider_spec(enabled_now)
    provider_adapter = resolve_provider_onboarding_adapter(enabled_now)
    if provider_adapter is None and key_env and not (provider_spec and provider_spec.is_oauth):
        provider_adapter = ApiKeyProviderOnboardingAdapter(
            provider_name=enabled_now,
            prompt=f"API key for {enabled_now} (recommended now, press Enter to skip for now)> ",
        )
    if provider_adapter is not None:
        provider_adapter.collect_credentials(
            provider_cfg=providers_cfg[enabled_now],
            input_fn=input_fn,
            secret_input_fn=secret_input_fn,
        )

    channels_cfg = config.get("channels", {})
    if isinstance(channels_cfg, dict):
        _render_install_interactive_snapshot_with_rich(title="Setup: Channels", config=config)
        channel_names = [
            str(name)
            for name, item in channels_cfg.items()
            if isinstance(item, dict) and "enabled" in item
        ]
        enabled_channels = [
            name for name in channel_names if bool(channels_cfg.get(name, {}).get("enabled"))
        ]
        if channel_names:
            channel_labels = [
                _channel_choice_label(
                    name=name,
                    cfg=channels_cfg.get(name, {}) if isinstance(channels_cfg.get(name, {}), dict) else {},
                    enabled_now=(name in enabled_channels),
                )
                for name in channel_names
            ]
            label_to_channel = {label: name for label, name in zip(channel_labels, channel_names)}
            raw_channels: list[str]
            if multi_select_fn is not None:
                defaults = [label for label in channel_labels if label_to_channel[label] in enabled_channels]
                raw_channels = [
                    str(item).strip()
                    for item in multi_select_fn("Enable channels (status shown as enabled/readiness)", channel_labels, defaults)
                ]
            else:
                _stdout_line(
                    "Install setup: choose enabled channels "
                    f"{channel_names} (comma separated, Enter keeps current)."
                )
                raw_text = input_fn("Channels> ").strip()
                raw_channels = _parse_csv_list(raw_text) if raw_text else []
            if raw_channels:
                resolved_channels: list[str] = []
                for raw in raw_channels:
                    selected = label_to_channel.get(raw)
                    if selected is None and raw in channel_names:
                        selected = raw
                    if selected and selected not in resolved_channels:
                        resolved_channels.append(selected)
                if not resolved_channels:
                    _stdout_line("Install setup: no valid channels selected, keep current channels.")
                else:
                    for name in channel_names:
                        channels_cfg[name]["enabled"] = name in resolved_channels
                    if not any(bool(channels_cfg.get(name, {}).get("enabled")) for name in channel_names) and "local" in channel_names:
                        channels_cfg["local"]["enabled"] = True
                    selected_labels = [
                        _channel_choice_label(
                            name=name,
                            cfg=channels_cfg.get(name, {}) if isinstance(channels_cfg.get(name, {}), dict) else {},
                            enabled_now=bool(channels_cfg.get(name, {}).get("enabled")),
                        )
                        for name in channel_names
                        if bool(channels_cfg.get(name, {}).get("enabled"))
                    ]
                    _stdout_line(
                        "Install setup: enabled channels -> "
                        + (", ".join(selected_labels) if selected_labels else "(none)")
                    )
        enabled_after = [name for name in channel_names if bool(channels_cfg.get(name, {}).get("enabled"))]
        fallback_channels: list[str] = []
        for channel_name in enabled_after:
            channel_cfg = channels_cfg.get(channel_name, {})
            if not isinstance(channel_cfg, dict):
                continue
            channel_adapter = resolve_channel_onboarding_adapter(channel_name)
            if channel_adapter is None:
                fallback_channels.append(channel_name)
                continue
            channel_adapter.collect_credentials(
                channel_cfg=channel_cfg,
                input_fn=input_fn,
                secret_input_fn=secret_input_fn,
            )
        if fallback_channels:
            install_rules.apply_install_channel_prompt_rules(
                channels_cfg=channels_cfg,
                enabled_channels=fallback_channels,
                input_fn=input_fn,
                secret_input_fn=secret_input_fn,
            )

    # Second guided pass: validate missing required fields and offer refill.
    if review_missing:
        _render_install_interactive_snapshot_with_rich(title="Setup: Review Missing Fields", config=config)
        _review_install_missing_fields(
            providers_cfg=providers_cfg if isinstance(providers_cfg, dict) else {},
            channels_cfg=channels_cfg if isinstance(channels_cfg, dict) else {},
            input_fn=input_fn,
            secret_input_fn=secret_input_fn,
        )
        _render_install_interactive_snapshot_with_rich(title="Setup: Final", config=config)

    save_config(config, config_path=config_path)
    _stdout_line(f"Install setup saved: {config_path}")


def _install_step_line(step: int, total: int, message: str) -> None:
    """Render one install step line, using rich style when available."""

    try:
        from rich import print as rich_print  # type: ignore

        rich_print(f"[bold cyan]Install step {step}/{total}:[/bold cyan] {message}")
    except Exception:
        _stdout_line(f"Install step {step}/{total}: {message}")


def _interactive_install_input(prompt: str) -> str:
    """Read one install input value, using questionary when available."""

    if _can_use_questionary():
        try:
            import questionary  # type: ignore

            answer = questionary.text(prompt).ask()
            return str(answer or "")
        except Exception:
            pass
    return input(prompt)


def _can_use_questionary() -> bool:
    """Return whether rich interactive questionary prompts should be used."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def _interactive_install_select(prompt: str, choices: list[str], default: str) -> str:
    """Select one install option, using questionary when available."""

    if _can_use_questionary():
        import questionary  # type: ignore

        try:
            answer = questionary.select(prompt, choices=choices, default=default).ask()
            return str(answer or "")
        except Exception:
            pass
    _stdout_line(prompt)
    numbered = list(enumerate(choices, start=1))
    if "provider" in prompt.lower():
        oauth_rows: list[tuple[int, str]] = []
        regular_rows: list[tuple[int, str]] = []
        for idx, choice in numbered:
            provider_token = choice.split(" ", 1)[0].strip()
            if canonical_provider_name(provider_token) in oauth_provider_names():
                oauth_rows.append((idx, choice))
            else:
                regular_rows.append((idx, choice))
        if regular_rows:
            _stdout_line("  API key providers:")
            for idx, choice in regular_rows:
                marker = " (default)" if choice == default else ""
                _stdout_line(f"    {idx}) {choice}{marker}")
        if oauth_rows:
            _stdout_line("  OAuth providers (login required, no API key input):")
            for idx, choice in oauth_rows:
                marker = " (default)" if choice == default else ""
                _stdout_line(f"    {idx}) {choice}{marker}")
            _stdout_line("    Use: openheron provider login <provider-name>")
    else:
        for idx, choice in numbered:
            marker = " (default)" if choice == default else ""
            _stdout_line(f"  {idx}) {choice}{marker}")
    raw = input("Select one (index/name, Enter=default)> ").strip()
    if not raw:
        return default
    try:
        index = int(raw)
    except Exception:
        index = -1
    if 1 <= index <= len(choices):
        return choices[index - 1]
    return raw


def _interactive_install_secret(prompt: str) -> str:
    """Read one secret install value, using questionary password when available."""

    if _can_use_questionary():
        try:
            import questionary  # type: ignore

            answer = questionary.password(prompt).ask()
            return str(answer or "")
        except Exception:
            pass
    return input(prompt)


def _interactive_install_multi_select(prompt: str, choices: list[str], defaults: list[str]) -> list[str]:
    """Read multi-select install options, using questionary when available."""

    if _can_use_questionary():
        try:
            import questionary  # type: ignore

            answer = questionary.checkbox(prompt, choices=choices, default=defaults).ask()
            if not answer:
                return []
            return [str(item) for item in answer]
        except Exception:
            pass
    _stdout_line(prompt)
    for idx, choice in enumerate(choices, start=1):
        marker = " (default)" if choice in defaults else ""
        _stdout_line(f"  {idx}) {choice}{marker}")
    raw = input("Select many (e.g. 1,3 or name1,name2; Enter=defaults)> ").strip()
    if not raw:
        return list(defaults)
    tokens = _parse_csv_list(raw)
    selected: list[str] = []
    for token in tokens:
        try:
            index = int(token)
        except Exception:
            index = -1
        if 1 <= index <= len(choices):
            item = choices[index - 1]
        else:
            item = token
        if item not in selected:
            selected.append(item)
    return selected


def _install_summary_lines(config_path: Path) -> list[str]:
    """Build short install summary lines for operator visibility."""

    config = load_config(config_path=config_path)
    provider_cfg = config.get("providers", {})
    selected_provider = "-"
    if isinstance(provider_cfg, dict):
        for name, item in provider_cfg.items():
            if isinstance(item, dict) and bool(item.get("enabled")):
                selected_provider = str(name)
                break

    channels_cfg = config.get("channels", {})
    enabled_channels: list[str] = []
    if isinstance(channels_cfg, dict):
        for name, item in channels_cfg.items():
            if isinstance(item, dict) and bool(item.get("enabled")):
                enabled_channels.append(str(name))
    channel_args = ",".join(enabled_channels) if enabled_channels else "local"
    gateway_cmd = f"openheron gateway --channels {channel_args}"

    provider_cfg_dict = provider_cfg if isinstance(provider_cfg, dict) else {}
    channels_cfg_dict = channels_cfg if isinstance(channels_cfg, dict) else {}
    missing = install_rules.install_summary_missing(
        selected_provider=selected_provider,
        provider_cfg=provider_cfg_dict,
        channels_cfg=channels_cfg_dict,
        provider_requirements=install_rules.INSTALL_PROVIDER_SUMMARY_REQUIREMENTS,
        channel_requirements=INSTALL_CHANNEL_SUMMARY_REQUIREMENTS,
    )

    lines = [f"Install summary: provider={selected_provider}, channels={enabled_channels or ['(none)']}"]
    if missing:
        lines.append(f"Install summary: missing={missing}")
        fix_hints = install_rules.install_summary_fix_hints(
            missing=missing,
            selected_provider=selected_provider,
            config_path=config_path,
            provider_requirements=install_rules.INSTALL_PROVIDER_SUMMARY_REQUIREMENTS,
            channel_requirements=INSTALL_CHANNEL_SUMMARY_REQUIREMENTS,
        )
        lines.append(f"Install summary: fixes={fix_hints}")
        lines.append("Install summary: next[1]=openheron doctor")
        lines.append(f"Install summary: next[2]={gateway_cmd}")
    else:
        lines.append("Install summary: no required fields missing for selected provider/channels.")
        lines.append("Install summary: next[1]=openheron doctor")
        lines.append(f"Install summary: next[2]={gateway_cmd}")
    return lines


def _install_gateway_channels(config_path: Path, preferred_channels: str | None) -> str:
    """Resolve gateway channels used by install follow-up and daemon install."""

    if preferred_channels and preferred_channels.strip():
        return ",".join(parse_enabled_channels(preferred_channels))

    config = load_config(config_path=config_path)
    channels_cfg = config.get("channels", {})
    enabled_channels: list[str] = []
    if isinstance(channels_cfg, dict):
        for name, item in channels_cfg.items():
            if isinstance(item, dict) and bool(item.get("enabled")):
                enabled_channels.append(str(name))
    return ",".join(enabled_channels) if enabled_channels else "local"


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


def _render_install_sections_with_rich(*, summary_lines: list[str], prereq_lines: list[str]) -> bool:
    """Render install summary/prereq sections with Rich when terminal supports it."""
    if not sys.stdout.isatty():
        return False
    try:
        from rich.console import Console  # type: ignore
        from rich.panel import Panel  # type: ignore
        from rich.table import Table  # type: ignore
    except Exception:
        return False

    console = Console()
    summary_table = Table(show_header=False, box=None, pad_edge=False)
    summary_table.add_column("key", style="bold cyan", no_wrap=True)
    summary_table.add_column("value", style="white")
    next_steps: list[str] = []
    missing_items: list[str] = []
    fix_items: list[str] = []
    notes: list[str] = []

    for line in summary_lines:
        raw = line.strip()
        if not raw.startswith("Install summary:"):
            notes.append(raw)
            continue
        content = raw[len("Install summary:") :].strip()
        if content.startswith("provider="):
            summary_table.add_row("Active", content)
        elif content.startswith("missing="):
            summary_table.add_row("Missing", content[len("missing=") :])
            value = content[len("missing=") :].strip()
            if value.startswith("[") and value.endswith("]"):
                body = value[1:-1].strip()
                if body:
                    missing_items = [item.strip().strip("'\"") for item in body.split(",") if item.strip()]
        elif content.startswith("fixes="):
            summary_table.add_row("Fixes", content[len("fixes=") :])
            value = content[len("fixes=") :].strip()
            if value.startswith("[") and value.endswith("]"):
                body = value[1:-1].strip()
                if body:
                    fix_items = [item.strip().strip("'\"") for item in body.split(",") if item.strip()]
        elif content.startswith("next["):
            next_steps.append(content.split("=", 1)[1].strip() if "=" in content else content)
        else:
            notes.append(content)

    if summary_table.row_count > 0:
        console.print(
            Panel(
                summary_table,
                title="[bold]Install Summary[/bold]",
                border_style="cyan",
            )
        )

    if missing_items:
        grouped_missing: dict[str, list[str]] = {"provider": [], "channel": [], "other": []}
        for item in missing_items:
            if item.startswith("channels."):
                grouped_missing["channel"].append(item)
            elif "." in item:
                grouped_missing["provider"].append(item)
            else:
                grouped_missing["other"].append(item)
        missing_table = Table(show_header=True, box=None, pad_edge=False)
        missing_table.add_column("Group", style="bold yellow", no_wrap=True)
        missing_table.add_column("Missing Field", style="yellow")
        for group_name in ("provider", "channel", "other"):
            for item in grouped_missing[group_name]:
                missing_table.add_row(group_name, item)
        console.print(Panel(missing_table, title="[bold]Required Fields[/bold]", border_style="yellow"))

    if fix_items:
        fix_table = Table(show_header=True, box=None, pad_edge=False)
        fix_table.add_column("Suggested Fix", style="green")
        for item in fix_items:
            fix_table.add_row(item)
        console.print(Panel(fix_table, title="[bold]Suggested Fixes[/bold]", border_style="green"))

    if prereq_lines:
        prereq_table = Table(show_header=True, box=None, pad_edge=False)
        prereq_table.add_column("Status", style="bold", no_wrap=True)
        prereq_table.add_column("Check")
        for line in prereq_lines:
            normalized = _doctor_install_prereq_line(line)
            status = "OK"
            style = "green"
            if "[warn]" in normalized:
                status = "WARN"
                style = "yellow"
            detail = normalized
            if ":" in normalized:
                detail = normalized.split(":", 1)[1].strip()
            prereq_table.add_row(f"[{style}]{status}[/{style}]", detail)
        console.print(Panel(prereq_table, title="[bold]Prerequisites[/bold]", border_style="magenta"))

    if next_steps:
        next_table = Table(show_header=False, box=None, pad_edge=False)
        next_table.add_column("step", style="bold cyan", no_wrap=True)
        next_table.add_column("command", style="white")
        for index, step in enumerate(next_steps, start=1):
            next_table.add_row(str(index), step)
        console.print(Panel(next_table, title="[bold]Next Commands[/bold]", border_style="blue"))

    for note in notes:
        if note:
            console.print(note)
    return True


def _render_install_welcome_with_rich(
    *,
    mode: str,
    config_path: Path,
    runtime_config_path: Path,
    install_daemon: bool,
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
    table.add_row("Daemon", "enabled" if install_daemon else "disabled")
    console.print(Panel(table, title="[bold]OpenHeron Install Wizard[/bold]", border_style="cyan"))
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
    non_interactive: bool,
    accept_risk: bool = False,
    install_daemon: bool = False,
    daemon_channels: str | None = None,
    init_only: bool = False,
) -> int:
    """Run a minimal installation flow for first-time local setup."""
    config_path = get_config_path()
    runtime_config_path = config_path.with_name("runtime.json")
    mode = "init-only" if init_only else ("non-interactive" if non_interactive else "interactive")
    _render_install_welcome_with_rich(
        mode=mode,
        config_path=config_path,
        runtime_config_path=runtime_config_path,
        install_daemon=install_daemon,
    )

    if init_only:
        if non_interactive or accept_risk or install_daemon or daemon_channels:
            _stdout_line(
                "Install init-only mode cannot be combined with non-interactive/risk/daemon options."
            )
            return 1
        _install_step_line(1, 1, "initializing config and workspace...")
        return _cmd_install_init_setup(force=force)

    if non_interactive and not accept_risk:
        _stdout_line("Non-interactive install requires explicit risk acknowledgement.")
        _stdout_line("Re-run with: openheron install --non-interactive --accept-risk")
        return 1

    total_steps = 3 if install_daemon else 2
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

    if not non_interactive:
        if sys.stdin.isatty():
            _run_install_interactive_setup(
                config_path=config_path,
                input_fn=_interactive_install_input,
                select_fn=_interactive_install_select,
                secret_input_fn=_interactive_install_secret,
                multi_select_fn=_interactive_install_multi_select,
                review_missing=True,
            )
        else:
            _stdout_line("Install setup skipped: non-interactive terminal.")
    summary_lines = _install_summary_lines(config_path)
    prereq_lines = _install_prereq_lines()
    if not _render_install_sections_with_rich(summary_lines=summary_lines, prereq_lines=prereq_lines):
        for line in summary_lines:
            _stdout_line(line)
        for line in prereq_lines:
            _stdout_line(line)

    bootstrap_env_from_config()
    _install_step_line(2, total_steps, "running environment checks...")
    doctor_code = _cmd_doctor(output_json=False, verbose=False)
    if doctor_code != 0:
        _render_install_step_outcome_with_rich(
            step=2, total=total_steps, outcome="fail", message="doctor reported issues"
        )
        _stdout_line("Install completed with issues. Fix the items above, then rerun `openheron doctor`.")
        _render_install_action_plan_with_rich(
            commands=["openheron doctor", "openheron install --non-interactive --accept-risk"],
            title="Retry Plan",
        )
        return 1
    _render_install_step_outcome_with_rich(
        step=2, total=total_steps, outcome="done", message="environment checks passed"
    )

    if install_daemon:
        channels_value = _install_gateway_channels(config_path, daemon_channels)
        _install_step_line(3, total_steps, "installing gateway daemon...")
        daemon_code = _cmd_gateway_service_install(force=force, channels=channels_value, enable=True)
        if daemon_code != 0:
            _render_install_step_outcome_with_rich(
                step=3, total=total_steps, outcome="warn", message="daemon setup failed (main install completed)"
            )
            _stdout_line("Install daemon setup failed. Main install is complete; please run daemon install manually.")
            _stdout_line(
                f"Install daemon retry: openheron gateway-service install --enable --channels {channels_value}"
            )
            _render_install_action_plan_with_rich(
                commands=[f"openheron gateway-service install --enable --channels {channels_value}"],
                title="Daemon Retry",
            )
        else:
            _render_install_step_outcome_with_rich(
                step=3, total=total_steps, outcome="done", message="daemon setup complete"
            )
            _stdout_line("Install daemon setup complete.")

    if not non_interactive:
        _stdout_line("Install complete. Next: run `openheron gateway`.")
    _render_install_action_plan_with_rich(
        commands=["openheron doctor", "openheron gateway"],
    )
    return 0


def _cmd_gateway_local(sender_id: str, chat_id: str) -> int:
    return _cmd_gateway(channels="local", sender_id=sender_id, chat_id=chat_id, interactive_local=True)


def _gateway_log_dir() -> Path:
    """Return gateway runtime/log directory under ~/.openheron/log."""
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


def _cmd_gateway_start(*, channels: str | None, sender_id: str, chat_id: str) -> int:
    """Start gateway in background and persist runtime state."""
    existing = _read_gateway_pid()
    if existing and _is_pid_running(existing):
        _stdout_line(f"Gateway service already running (pid={existing}).")
        _stdout_line("Use `openheron gateway status` or `openheron gateway restart`.")
        return 0

    if existing and not _is_pid_running(existing):
        _gateway_cleanup_runtime_files()

    channels_value = ",".join(parse_enabled_channels(channels))
    cmd = [
        sys.executable,
        "-m",
        "openheron.app.cli",
        "gateway",
        "--channels",
        channels_value,
        "--sender-id",
        sender_id,
        "--chat-id",
        chat_id,
    ]
    env = dict(os.environ)
    env["OPENHERON_GATEWAY_BG"] = "1"
    env["OPENHERON_DEBUG_LOG_PATH"] = str(_gateway_debug_log_path())

    stdout_path = _gateway_stdout_log_path()
    stderr_path = _gateway_stderr_log_path()
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("a", encoding="utf-8") as stdout_fh, stderr_path.open("a", encoding="utf-8") as stderr_fh:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=stdout_fh,
                stderr=stderr_fh,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        except Exception as exc:
            _stdout_line(f"Gateway service start failed: {exc}")
            return 1

    _gateway_pid_path().write_text(f"{proc.pid}\n", encoding="utf-8")
    _write_gateway_runtime_metadata(pid=proc.pid, channels=channels_value, command=cmd)
    _stdout_line(f"Gateway service started (pid={proc.pid}).")
    _stdout_line(f"Logs: {_gateway_log_dir()}")
    return 0


def _cmd_gateway_stop(*, timeout_seconds: float = 8.0) -> int:
    """Stop gateway background process by pid file."""
    pid = _read_gateway_pid()
    if not pid:
        _stdout_line("Gateway service is not running (no pid file).")
        return 0
    if not _is_pid_running(pid):
        _gateway_cleanup_runtime_files()
        _stdout_line("Gateway service is not running (stale pid file removed).")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as exc:
        _stdout_line(f"Gateway service stop failed: {exc}")
        return 1

    deadline = time.time() + max(1.0, timeout_seconds)
    while time.time() < deadline:
        if not _is_pid_running(pid):
            _gateway_cleanup_runtime_files()
            _stdout_line("Gateway service stopped.")
            return 0
        time.sleep(0.15)

    try:
        os.kill(pid, signal.SIGKILL)
    except Exception as exc:
        _stdout_line(f"Gateway service stop timeout and kill failed: {exc}")
        return 1

    _gateway_cleanup_runtime_files()
    _stdout_line("Gateway service stopped (forced).")
    return 0


def _cmd_gateway_status(*, output_json: bool) -> int:
    """Show gateway background process status."""
    pid = _read_gateway_pid()
    running = bool(pid and _is_pid_running(pid))
    if pid and not running:
        _gateway_cleanup_runtime_files()
        pid = None
    meta = _read_gateway_runtime_metadata()
    payload: dict[str, Any] = {
        "running": running,
        "pid": pid,
        "logsDir": str(_gateway_log_dir()),
        "stdoutLog": str(_gateway_stdout_log_path()),
        "stderrLog": str(_gateway_stderr_log_path()),
        "debugLog": str(_gateway_debug_log_path()),
    }
    if meta:
        payload["meta"] = meta

    if output_json:
        _stdout_line(json.dumps(payload, ensure_ascii=False))
        return 0

    if not running:
        _stdout_line("Gateway service status: stopped")
        _stdout_line(f"Logs directory: {payload['logsDir']}")
        return 0

    _stdout_line(
        "Gateway service status: "
        f"running pid={pid}, logs={payload['logsDir']}"
    )
    if isinstance(meta, dict):
        channels = str(meta.get("channels", "")).strip()
        started_at = str(meta.get("startedAt", "")).strip()
        if channels:
            _stdout_line(f"Channels: {channels}")
        if started_at:
            _stdout_line(f"Started at: {started_at}")
    return 0


def _cmd_gateway_restart(*, channels: str | None, sender_id: str, chat_id: str) -> int:
    """Restart gateway background process."""
    stop_code = _cmd_gateway_stop()
    if stop_code != 0:
        return stop_code
    return _cmd_gateway_start(channels=channels, sender_id=sender_id, chat_id=chat_id)


def _required_mcp_servers_from_env() -> list[str]:
    """Read strong-dependency MCP server names from environment."""
    return _parse_csv_list(os.getenv("OPENHERON_MCP_REQUIRED_SERVERS", ""))


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
        if isinstance(tool, ManagedMcpToolset) and tool.meta.name in required_set
    ]
    if not required_toolsets:
        return issues

    mcp_probe_policy = _load_mcp_probe_policy(
        timeout_env_name="OPENHERON_MCP_GATEWAY_TIMEOUT_SECONDS",
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
        cfg = load_config()
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
            config=cfg,
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


def _format_schedule(job) -> str:
    return format_schedule(getattr(job, "schedule", None))


def _format_ts(ms: int | None) -> str:
    return format_timestamp_ms(ms)


def _cmd_cron_list(*, include_disabled: bool) -> int:
    service = _cron_service()
    jobs = service.list_jobs(include_disabled=include_disabled)
    if not jobs:
        _stdout_line("No scheduled jobs.")
        return 0
    _stdout_line("Scheduled jobs:")
    for job in jobs:
        status = "enabled" if job.enabled else "disabled"
        _stdout_line(
            f"- {job.name} (id: {job.id}, {_format_schedule(job)}, {status}, next={_format_ts(job.state.next_run_at_ms)})"
        )
    return 0


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
    if tz and not cron_expr:
        _stdout_line("Error: --tz can only be used with --cron")
        return 1
    if deliver and not to:
        _stdout_line("Error: --to is required when --deliver is set")
        return 1

    parsed, parse_error = parse_schedule_input(
        every_seconds=every,
        cron_expr=cron_expr,
        at=at,
        tz=tz,
    )
    if parse_error:
        _stdout_line(f"Error: {parse_error}")
        return 1
    if parsed is None:  # pragma: no cover - defensive fallback
        _stdout_line("Error: failed to parse schedule")
        return 1
    schedule = parsed.schedule
    delete_after_run = parsed.delete_after_run

    target_channel = channel or "local"
    target_to = to or "default"
    job = _cron_service().add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=deliver,
        channel=target_channel,
        to=target_to,
        delete_after_run=delete_after_run,
    )
    _stdout_line(f"Added job '{job.name}' ({job.id})")
    return 0


def _cmd_cron_remove(job_id: str) -> int:
    if _cron_service().remove_job(job_id):
        _stdout_line(f"Removed job {job_id}")
        return 0
    _stdout_line(f"Job {job_id} not found")
    return 1


def _cmd_cron_enable(job_id: str, *, disable: bool) -> int:
    job = _cron_service().enable_job(job_id, enabled=not disable)
    if job is None:
        _stdout_line(f"Job {job_id} not found")
        return 1
    state = "disabled" if disable else "enabled"
    _stdout_line(f"Job '{job.name}' {state}")
    return 0


def _cmd_cron_run(job_id: str, *, force: bool) -> int:
    async def _run():
        return await _cron_service().run_job_with_result(job_id, force=force)

    result = asyncio.run(_run())
    if result.reason == "ok":
        _stdout_line("Job executed")
        return 0
    if result.reason == "disabled":
        _stdout_line(f"Job {job_id} is disabled. Use --force to run it once.")
        return 1
    if result.reason == "not_found":
        _stdout_line(f"Job {job_id} not found")
        return 1
    if result.reason == "no_callback":
        _stdout_line(
            "Job skipped: no executor callback is configured in this process. "
            "Run via gateway runtime to execute the agent task."
        )
        return 1
    if result.reason == "error":
        if result.error:
            _stdout_line(f"Job execution failed: {result.error}")
        else:
            _stdout_line(f"Job execution failed: {job_id}")
        return 1
    _stdout_line(f"Job skipped: {result.reason}")
    return 1


def _cmd_cron_status() -> int:
    info = _cron_service().status()
    runtime_pid = info.get("runtime_pid")
    runtime_pid_text = str(runtime_pid) if runtime_pid is not None else "-"
    _stdout_line(
        "Cron status: "
        f"local_running={info['running']}, "
        f"runtime_active={info.get('runtime_active', False)}, "
        f"runtime_pid={runtime_pid_text}, "
        f"jobs={info['jobs']}, "
        f"next_wake_at={_format_ts(info['next_wake_at_ms'])}"
    )
    return 0


def _dispatch_cron_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch cron subcommands from parsed argparse namespace."""
    cron_handlers: dict[str, Callable[[], int]] = {
        "list": lambda: _cmd_cron_list(include_disabled=args.all),
        "add": lambda: _cmd_cron_add(
            name=args.name,
            message=args.message,
            every=args.every,
            cron_expr=args.cron_expr,
            tz=args.tz,
            at=args.at,
            deliver=args.deliver,
            to=args.to,
            channel=args.channel,
        ),
        "remove": lambda: _cmd_cron_remove(args.job_id),
        "enable": lambda: _cmd_cron_enable(args.job_id, disable=args.disable),
        "run": lambda: _cmd_cron_run(args.job_id, force=args.force),
        "status": _cmd_cron_status,
    }
    handler = cron_handlers.get(args.cron_command)
    if handler is None:
        parser.print_help()
        return 2
    return handler()


def _cmd_heartbeat_status(*, output_json: bool) -> int:
    workspace = load_security_policy().workspace_root
    snapshot = read_heartbeat_status_snapshot(workspace)
    if output_json:
        _stdout_line(json.dumps(snapshot or {}, ensure_ascii=False))
        return 0
    if not snapshot:
        _stdout_line("Heartbeat status: no runtime snapshot yet.")
        return 0
    delivery = snapshot.get("last_delivery")
    delivery_kind = delivery.get("kind") if isinstance(delivery, dict) else "-"
    _stdout_line(
        "Heartbeat status: "
        f"running={snapshot.get('running', False)}, "
        f"enabled={snapshot.get('enabled', False)}, "
        f"last_status={snapshot.get('last_status', '-')}, "
        f"last_reason={snapshot.get('last_reason', '-')}, "
        f"target_mode={snapshot.get('target_mode', '-')}, "
        f"last_delivery_kind={delivery_kind}"
    )
    _stdout_line(
        f"Heartbeat recent reasons: {json.dumps(snapshot.get('recent_reason_counts', {}), ensure_ascii=False)}"
    )
    return 0


def _cmd_token_stats(
    *,
    output_json: bool,
    limit: int,
    provider: str | None,
    since: str | None,
    until: str | None,
    last_hours: int | None,
) -> int:
    """Show aggregated LLM token usage and recent request records."""
    since_ms: int | None = None
    until_ms: int | None = None
    if last_hours is not None:
        now_ms = int(time.time() * 1000)
        since_ms = now_ms - (max(1, int(last_hours)) * 3600 * 1000)
        until_ms = now_ms
    else:
        try:
            since_ms = parse_time_filter_to_epoch_ms(since)
            until_ms = parse_time_filter_to_epoch_ms(until)
        except ValueError as exc:
            _stdout_line(f"Error: invalid --since/--until value ({exc})")
            return 1
    if since_ms is not None and until_ms is not None and since_ms > until_ms:
        _stdout_line("Error: --since must be earlier than or equal to --until")
        return 1

    stats = read_token_usage_stats(
        limit=limit,
        provider=provider or None,
        since_ms=since_ms,
        until_ms=until_ms,
    )
    payload: dict[str, Any] = {
        "dbPath": str(token_usage_db_path()),
        "provider": provider or "",
        "since": since or "",
        "until": until or "",
        "lastHours": int(last_hours) if last_hours is not None else None,
        **stats,
    }
    if output_json:
        _stdout_line(json.dumps(payload, ensure_ascii=False))
        return 0

    _stdout_line(
        "Token stats: "
        f"requests={stats['requests']}, "
        f"request_tokens={stats['request_tokens']}, "
        f"response_tokens={stats['response_tokens']}, "
        f"total_tokens={stats['total_tokens']}"
    )
    if last_hours is not None:
        _stdout_line(f"Time range: last_hours={int(last_hours)}")
    elif since or until:
        _stdout_line(f"Time range: since={since or '-'}, until={until or '-'}")
    _stdout_line(
        "Token by modality: "
        f"request(text={stats['request_text_tokens']}, image={stats['request_image_tokens']}), "
        f"response(text={stats['response_text_tokens']}, image={stats['response_image_tokens']})"
    )
    _stdout_line(f"Token DB: {payload['dbPath']}")
    if not stats["recent"]:
        _stdout_line("Recent: no records")
        return 0
    _stdout_line("Recent records:")
    for row in stats["recent"]:
        _stdout_line(
            "- "
            f"{row.get('response_at', '-')}"
            f" provider={row.get('provider', '-')}"
            f" model={row.get('model', '-')}"
            f" session={row.get('session_id', '-')}"
            f" req={row.get('request_tokens', 0)}"
            f" resp={row.get('response_tokens', 0)}"
            f" total={row.get('total_tokens', 0)}"
            f" req_img={row.get('request_image_tokens', 0)}"
            f" resp_img={row.get('response_image_tokens', 0)}"
        )
    return 0


def _dispatch_token_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch token subcommands from parsed argparse namespace."""
    token_handlers: dict[str, Callable[[], int]] = {
        "stats": lambda: _cmd_token_stats(
            output_json=args.output_json,
            limit=args.limit,
            provider=args.provider,
            since=args.since,
            until=args.until,
            last_hours=args.last_hours,
        ),
    }
    handler = token_handlers.get(args.token_command)
    if handler is None:
        parser.print_help()
        return 2
    return handler()


def _gateway_service_manifest_path(manager: str, service_name: str) -> Path:
    """Return user-level gateway service manifest path for one service manager."""

    if manager == "launchd":
        return Path.home() / "Library" / "LaunchAgents" / f"{service_name}.plist"
    return Path.home() / ".config" / "systemd" / "user" / f"{service_name}.service"


def _gateway_service_exec_argv(channels: str) -> tuple[str, list[str]]:
    """Return executable argv used by generated gateway service manifests."""

    openheron_bin = shutil.which("openheron")
    if openheron_bin:
        return openheron_bin, ["gateway", "--channels", channels]
    return sys.executable, ["-m", "openheron.app.cli", "gateway", "--channels", channels]


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
    service_name = gateway_service_name("openheron")
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
            env={"OPENHERON_CHANNELS": channels_value},
            stdout_path=stdout_log,
            stderr_path=stderr_log,
        )
        enable_hint = f"launchctl load -w {manifest_path}"
        disable_hint = f"launchctl unload {manifest_path}"
    else:
        exec_start = " ".join([program, *args])
        manifest = render_systemd_unit(
            description="Openheron Gateway Service",
            exec_start=exec_start,
            working_directory=workspace,
            env={"OPENHERON_CHANNELS": channels_value},
        )
        enable_hint = f"systemctl --user daemon-reload && systemctl --user enable --now {service_name}.service"
        disable_hint = f"systemctl --user disable --now {service_name}.service"

    manifest_path.write_text(manifest, encoding="utf-8")
    _stdout_line("Gateway service install: this config lets OS user service manager run `openheron gateway`.")
    _stdout_line(f"Gateway service manifest written: {manifest_path}")
    _stdout_line(f"Gateway service manager: {manager}")
    _stdout_line(f"Gateway service channels: {channels_value}")
    _stdout_line(f"Gateway service logs: stdout={stdout_log}, stderr={stderr_log}")
    _stdout_line(f"Next step (enable/start): {enable_hint}")
    _stdout_line(f"Stop/disable command: {disable_hint}")
    _stdout_line("Tip: use `openheron gateway status` for process status, `openheron gateway-service status` for manifest state.")
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
    service_name = gateway_service_name("openheron")
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
                _stdout_line("Manifest is ready. If service is not running yet, run `openheron gateway-service install --enable`.")
            else:
                _stdout_line("Manifest not found. Run `openheron gateway-service install` first.")
            _stdout_line("Note: this checks service manifest only; use `openheron gateway status` for process runtime state.")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="openheron",
        description="Lightweight skills-only agent based on Google ADK.",
    )
    parser.add_argument("-m", "--message", help="Run a single-turn request and print the response.")
    parser.add_argument("--user-id", default="local-user", help="User id for ADK session mode.")
    parser.add_argument(
        "--session-id",
        default="",
        help="Session id for ADK session mode (auto-generated if omitted).",
    )

    subparsers = parser.add_subparsers(dest="command", required=False)
    install_parser = subparsers.add_parser(
        "install",
        help="Run guided installation and setup checks. See `openheron install --help` for full options.",
        description=(
            "Guided installer: initialize config/workspace, optional interactive setup, "
            "doctor checks, and next-step hints."
        ),
    )
    install_parser.add_argument(
        "--init-only",
        action="store_true",
        help="Initialize config/workspace only.",
    )
    install_parser.add_argument(
        "--force",
        action="store_true",
        help="Reset config to defaults before running checks.",
    )
    install_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip interactive setup prompts (still prints install summary and next-step commands).",
    )
    install_parser.add_argument(
        "--accept-risk",
        action="store_true",
        help="Acknowledge non-interactive install risks (required with --non-interactive).",
    )
    install_parser.add_argument(
        "--install-daemon",
        action="store_true",
        help="Install and enable user-level gateway daemon (launchd/systemd user).",
    )
    install_parser.add_argument(
        "--daemon-channels",
        default=None,
        help="Comma-separated channels for daemon mode (default: enabled channels in config).",
    )
    subparsers.add_parser("skills", help="List discovered skills as JSON.")
    subparsers.add_parser("mcps", help="List connected MCP servers and their available APIs.")
    subparsers.add_parser("spawn", help="List recent sub-agent tasks created by spawn_subagent.")
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

    run_parser = subparsers.add_parser("run", help="Run `adk run` for this agent.")
    run_parser.add_argument("adk_args", nargs=argparse.REMAINDER, help="Extra args passed to adk run.")
    gateway_parser = subparsers.add_parser(
        "gateway-local",
        help="Run minimal local channel gateway (bus + runner + stdio).",
    )
    gateway_parser.add_argument("--sender-id", default="local-user", help="Sender id used for inbound messages.")
    gateway_parser.add_argument("--chat-id", default="terminal", help="Chat id used for inbound messages.")
    gateway_parser = subparsers.add_parser(
        "gateway",
        help="Run gateway in foreground, or start/stop/restart/status background gateway process.",
        description=(
            "Gateway runtime command. Without action it runs in foreground; with action "
            "it manages the background gateway process."
        ),
    )
    gateway_parser.add_argument(
        "gateway_action",
        nargs="?",
        choices=["start", "stop", "restart", "status"],
        help="Background gateway action (omit for foreground run).",
    )
    gateway_parser.add_argument(
        "--channels",
        default=None,
        help="Comma-separated channels. Defaults to OPENHERON_CHANNELS or 'local'.",
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
    provider_parser = subparsers.add_parser("provider", help="Manage runtime LLM providers.")
    provider_subparsers = provider_parser.add_subparsers(dest="provider_command", required=True)
    provider_subparsers.add_parser("list", help="List providers available to openheron.")
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
    routes_parser = subparsers.add_parser("routes", help="Route diagnostics helpers.")
    routes_subparsers = routes_parser.add_subparsers(dest="routes_command", required=True)
    routes_lint_parser = routes_subparsers.add_parser("lint", help="Lint multi-agent routing config.")
    routes_lint_parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit routes lint result as one machine-readable JSON object.",
    )
    routes_lint_parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="Max route preview items in output (default: 8).",
    )

    channels_parser = subparsers.add_parser("channels", help="Manage channel helper commands.")
    channels_subparsers = channels_parser.add_subparsers(dest="channels_command", required=True)
    channels_login_parser = channels_subparsers.add_parser("login", help="Start QR login helper for a channel.")
    channels_login_parser.add_argument(
        "channel_name",
        nargs="?",
        default="whatsapp",
        help="Channel name (default: whatsapp).",
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
    cron_subparsers = cron_parser.add_subparsers(dest="cron_command", required=True)

    cron_list_parser = cron_subparsers.add_parser("list", help="List scheduled cron jobs.")
    cron_list_parser.add_argument("--all", action="store_true", help="Include disabled jobs.")

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
    token_parser = subparsers.add_parser("token", help="Token usage runtime helpers.")
    token_subparsers = token_parser.add_subparsers(dest="token_command", required=True)
    token_stats_parser = token_subparsers.add_parser("stats", help="Show token usage stats.")
    token_stats_parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit token stats as one machine-readable JSON object.",
    )
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
        help="Optional inclusive start time in ISO8601, e.g. 2026-02-26T00:00:00+08:00.",
    )
    token_stats_parser.add_argument(
        "--until",
        default=None,
        help="Optional inclusive end time in ISO8601, e.g. 2026-02-26T23:59:59+08:00.",
    )
    token_stats_parser.add_argument(
        "--last-hours",
        type=int,
        default=None,
        help="Shortcut time filter for recent N hours (overrides --since/--until).",
    )
    gateway_service_parser = subparsers.add_parser(
        "gateway-service",
        help="Manage OS user service config (launchd/systemd) that runs `openheron gateway`.",
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
            "Write launchd/systemd user manifest that executes `openheron gateway --channels ...`.\n"
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
        description="Inspect launchd/systemd user manifest state for openheron gateway service.",
    )
    gateway_service_status_parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit gateway service status as one machine-readable JSON object.",
    )

    args = parser.parse_args(argv)
    if args.command != "install":
        bootstrap_env_from_config()

    # Global `-m/--message` is single-turn mode only when no subcommand is used.
    if args.command is None and args.message:
        sid = args.session_id or uuid.uuid4().hex[:12]
        code = _cmd_message(args.message, user_id=args.user_id, session_id=sid)
    elif args.command == "cron":
        code = _dispatch_cron_command(args, parser)
    elif args.command == "heartbeat":
        if args.heartbeat_command == "status":
            code = _cmd_heartbeat_status(output_json=args.output_json)
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
    elif args.command == "channels":
        code = _dispatch_channels_command(args, parser)
    elif args.command == "provider":
        code = _dispatch_provider_command(args, parser)
    elif args.command == "routes":
        code = _dispatch_routes_command(args, parser)
    else:
        handlers: dict[str, Callable[[], int]] = {
            "install": lambda: _cmd_install(
                force=args.force,
                non_interactive=args.non_interactive,
                accept_risk=args.accept_risk,
                install_daemon=args.install_daemon,
                daemon_channels=args.daemon_channels,
                init_only=args.init_only,
            ),
            "skills": _cmd_skills,
            "mcps": _cmd_mcps,
            "spawn": _cmd_spawn,
            "doctor": lambda: _cmd_doctor(
                output_json=args.output_json,
                verbose=args.verbose,
                fix=(args.fix or args.fix_dry_run),
                fix_dry_run=args.fix_dry_run,
            ),
            "run": lambda: _cmd_run(args.adk_args),
            "gateway-local": lambda: _cmd_gateway_local(sender_id=args.sender_id, chat_id=args.chat_id),
            "gateway": lambda: (
                _cmd_gateway_start(channels=args.channels, sender_id=args.sender_id, chat_id=args.chat_id)
                if args.gateway_action == "start"
                else _cmd_gateway_stop()
                if args.gateway_action == "stop"
                else _cmd_gateway_restart(channels=args.channels, sender_id=args.sender_id, chat_id=args.chat_id)
                if args.gateway_action == "restart"
                else _cmd_gateway_status(output_json=args.output_json)
                if args.gateway_action == "status"
                else _cmd_gateway(
                    channels=args.channels,
                    sender_id=args.sender_id,
                    chat_id=args.chat_id,
                    interactive_local=args.interactive_local,
                )
            ),
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
