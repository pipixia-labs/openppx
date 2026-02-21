"""Minimal CLI helpers for openheron."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import signal
import socket
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from google.genai import types
from loguru import logger

from .channels.factory import build_channel_manager, parse_enabled_channels, validate_channel_setup
from .config import (
    bootstrap_env_from_config,
    default_config,
    get_data_dir,
    get_config_path,
    load_config,
    save_config,
)
from .env_utils import env_enabled
from .logging_utils import debug_logging_enabled, emit_debug
from .mcp_registry import ManagedMcpToolset, build_mcp_toolsets_from_env, probe_mcp_toolsets, summarize_mcp_toolsets
from .provider import (
    canonical_provider_name,
    normalize_model_name,
    normalize_provider_name,
    oauth_provider_names,
    provider_api_key_env,
    provider_names,
    validate_provider_runtime,
)
from .provider_registry import find_provider_spec
from .runtime.adk_utils import extract_text, merge_text_stream
from .runtime.cron_helpers import cron_store_path, format_schedule, format_timestamp_ms
from .runtime.cron_service import CronService
from .runtime.cron_schedule_parser import parse_schedule_input
from .runtime.message_time import inject_request_time
from .runtime.runner_factory import create_runner
from .runtime.session_service import load_session_config
from .security import load_security_policy
from .skills import get_registry


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
    logger.info(json.dumps(payload, ensure_ascii=False, indent=2))
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
        logger.info("MCP: no servers configured")
        return 0

    probe_policy = _load_mcp_probe_policy(
        timeout_env_name="OPENHERON_MCP_LIST_TIMEOUT_SECONDS",
        timeout_default=5.0,
    )
    try:
        results = asyncio.run(
            probe_mcp_toolsets(
                toolsets,
                timeout_seconds=probe_policy.timeout_seconds,
                retry_attempts=probe_policy.retry_attempts,
                retry_backoff_seconds=probe_policy.retry_backoff_seconds,
            )
        )
    except Exception as exc:
        logger.info(f"MCP probe failed: {exc}")
        return 1

    toolsets_by_name = {toolset.meta.name: toolset for toolset in toolsets}
    connected_names = [str(item.get("name", "")) for item in results if str(item.get("status")) == "ok"]
    if not connected_names:
        logger.info("MCP: no connected servers")
        return 0

    connected_toolsets = [toolsets_by_name[name] for name in connected_names if name in toolsets_by_name]
    api_names_by_server = asyncio.run(
        _collect_connected_mcp_apis(
            connected_toolsets,
            timeout_seconds=probe_policy.timeout_seconds,
        )
    )

    logger.info(f"Connected MCP servers: {len(connected_toolsets)}")
    for item in results:
        if str(item.get("status")) != "ok":
            continue
        server_name = str(item.get("name", "unknown"))
        transport = str(item.get("transport", "unknown"))
        api_rows = api_names_by_server.get(server_name, [])
        logger.info(f"- {server_name} ({transport})")
        if api_rows:
            logger.info(f"  APIs ({len(api_rows)}):")
            for api in api_rows:
                logger.info(f"  - {api.get('name', '')}")
                logger.info(f"    功能: {api.get('description', '(未提供)')}")
                logger.info(f"    输入: {api.get('input', '(未声明)')}")
                logger.info(f"    输出: {api.get('output', '(未声明)')}")
        else:
            logger.info("  APIs (0): (none)")
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
        logger.info("Subagents: none")
        return 0

    logger.info(f"Subagents: {len(records)} recent task(s)")
    for item in records:
        task_id = str(item.get("task_id", "unknown"))
        status = str(item.get("status", "unknown"))
        channel = str(item.get("channel", "unknown"))
        chat_id = str(item.get("chat_id", "unknown"))
        created_at = str(item.get("timestamp", ""))
        prompt_preview = str(item.get("prompt_preview", "")).strip()
        logger.info(
            f"- {task_id} status={status} target={channel}:{chat_id} created_at={created_at}"
        )
        if prompt_preview:
            logger.info(f"  prompt: {prompt_preview}")
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


def _cmd_doctor(*, output_json: bool = False, verbose: bool = False) -> int:
    """Run runtime diagnostics and return a process exit code.

    When `output_json` is true, emits one machine-readable JSON payload.
    """
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

    config_path = get_config_path()
    registry = get_registry()
    skills_count = len(registry.list_skills())
    session_cfg = load_session_config()
    configured_channels = parse_enabled_channels(None)
    channel_issues = validate_channel_setup(configured_channels)
    issues.extend(channel_issues)
    if "whatsapp" in configured_channels and _whatsapp_bridge_precheck_enabled():
        bridge_issue = _check_whatsapp_bridge_ready()
        if bridge_issue:
            issues.append(bridge_issue)
    web_enabled = env_enabled("OPENHERON_WEB_ENABLED", default=True)
    web_search_enabled = env_enabled("OPENHERON_WEB_SEARCH_ENABLED", default=True)
    web_search_provider = os.getenv("OPENHERON_WEB_SEARCH_PROVIDER", "brave").strip().lower() or "brave"
    web_search_key_configured = bool(os.getenv("BRAVE_API_KEY", "").strip())
    security_policy = load_security_policy()
    mcp_toolsets = build_mcp_toolsets_from_env(log_registered=False)
    mcp_summaries = summarize_mcp_toolsets(mcp_toolsets)
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
        "skills": {"count": skills_count},
        "session": {"db_url": session_cfg.db_url},
        "channels": {"configured": configured_channels},
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
        logger.info("Doctor details:")
        logger.info(json.dumps(report, ensure_ascii=False, indent=2))

    if issues:
        logger.info("Issues:")
        for item in issues:
            logger.info(f"- {item}")
        return 1

    logger.info("Environment looks good.")
    return 0


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
        logger.info("Starting interactive OAuth login for OpenAI Codex...")
        token = login_oauth_interactive(
            print_fn=lambda s: _stdout_line(str(s)),
            prompt_fn=lambda s: input(str(s)),
        )

    ok, detail = _check_openai_codex_oauth()
    if not ok:
        raise RuntimeError(f"OpenAI Codex authentication failed ({detail}).")

    account_id = str(getattr(token, "account_id", "")).strip()
    logger.info(f"OpenAI Codex OAuth authenticated ({account_id}).")


@_register_provider_login("github_copilot")
def _provider_login_github_copilot() -> None:
    """Authenticate GitHub Copilot via LiteLLM device flow."""
    logger.info("Starting GitHub Copilot OAuth device flow...")

    async def _trigger() -> None:
        from litellm import acompletion

        await acompletion(
            model="github_copilot/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )

    asyncio.run(_trigger())
    logger.info("GitHub Copilot OAuth authenticated.")


def _cmd_provider_list() -> int:
    """List providers known by the runtime."""
    logger.info("Providers:")
    for name in provider_names():
        spec = find_provider_spec(name)
        if not spec:
            logger.info(f"- {name}")
            continue
        oauth_flag = ", oauth=true" if spec.is_oauth else ""
        logger.info(
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

    logger.info(
        f"Provider: {provider_name} (enabled={provider_enabled}, model={provider_model})"
    )
    if provider_key_env:
        logger.info(f"API key: {provider_key_env}={'configured' if provider_key_configured else 'missing'}")
    else:
        logger.info("API key: not required")
    logger.info(
        "OAuth: "
        f"required={provider_oauth.get('required')}, "
        f"authenticated={provider_oauth.get('authenticated')}, "
        f"message={provider_oauth.get('message')}"
    )
    if issues:
        logger.info("Issues:")
        for issue in issues:
            logger.info(f"- {issue}")
        return 1
    logger.info("Provider is ready.")
    return 0


def _cmd_provider_login(provider_name: str) -> int:
    """Authenticate an OAuth provider account for local runtime use."""
    normalized = canonical_provider_name(provider_name)
    spec = find_provider_spec(normalized)
    oauth_names = ", ".join(name.replace("_", "-") for name in oauth_provider_names())
    if spec is None or not spec.is_oauth:
        logger.info(
            f"Unknown OAuth provider '{provider_name}'. "
            f"Supported providers: {oauth_names}"
        )
        return 1

    handler = _PROVIDER_LOGIN_HANDLERS.get(spec.name)
    if handler is None:
        logger.info(f"OAuth login is not implemented for provider '{provider_name}'.")
        return 1

    try:
        handler()
    except Exception as exc:
        logger.info(f"OAuth login failed for {provider_name}: {exc}")
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
        logger.info(f"Unsupported channel for login: {channel_name}. Supported: whatsapp")
        return 1

    try:
        bridge_dir = _get_bridge_dir()
    except RuntimeError as exc:
        logger.info(str(exc))
        return 1
    except Exception as exc:
        logger.info(f"Failed to prepare bridge directory: {exc}")
        return 1

    env = dict(os.environ)
    bridge_token = _whatsapp_bridge_token_from_config(load_config())
    if bridge_token:
        env["BRIDGE_TOKEN"] = bridge_token

    logger.info("Starting WhatsApp bridge. Scan the QR code in this terminal to connect.")
    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as exc:
        logger.info(f"Bridge failed: {exc}")
        return 1
    except FileNotFoundError:
        logger.info("npm not found. Please install Node.js >= 20.")
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
        logger.info(f"Unsupported channel bridge: {channel_name}. Supported: whatsapp")
        return 1

    state = _read_bridge_runtime_state()
    if state:
        existing_pid = _state_pid(state)
        if existing_pid and _is_pid_running(existing_pid):
            logger.info(f"Bridge is already running (pid={existing_pid}).")
            return 0
        _clear_bridge_runtime_state()

    try:
        bridge_dir = _get_bridge_dir()
    except RuntimeError as exc:
        logger.info(str(exc))
        return 1
    except Exception as exc:
        logger.info(f"Failed to prepare bridge directory: {exc}")
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
        logger.info("npm not found. Please install Node.js >= 20.")
        return 1
    except Exception as exc:
        logger.info(f"Failed to start bridge: {exc}")
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
    logger.info(f"Bridge started in background (pid={proc.pid}).")
    return 0


def _cmd_channels_bridge_status(*, channel_name: str) -> int:
    """Print bridge runtime status."""
    target = channel_name.strip().lower()
    if target != "whatsapp":
        logger.info(f"Unsupported channel bridge: {channel_name}. Supported: whatsapp")
        return 1

    state = _read_bridge_runtime_state()
    if not state:
        logger.info("Bridge is not running (no runtime state).")
        return 0

    pid = _state_pid(state)
    if pid and _is_pid_running(pid):
        logger.info(f"Bridge is running (pid={pid}).")
        return 0
    logger.info("Bridge is not running (stale runtime state found).")
    return 0


def _cmd_channels_bridge_stop(*, channel_name: str) -> int:
    """Stop background bridge process tracked by runtime state."""
    target = channel_name.strip().lower()
    if target != "whatsapp":
        logger.info(f"Unsupported channel bridge: {channel_name}. Supported: whatsapp")
        return 1

    state = _read_bridge_runtime_state()
    if not state:
        logger.info("Bridge is not running.")
        return 0

    pid = _state_pid(state)
    if pid is None:
        _clear_bridge_runtime_state()
        logger.info("Bridge runtime state was invalid and has been cleared.")
        return 0

    if not _is_pid_running(pid):
        _clear_bridge_runtime_state()
        logger.info(f"Bridge is not running (stale pid={pid} removed).")
        return 0

    if not _stop_bridge_pid(pid):
        logger.info(f"Failed to stop bridge process pid={pid}.")
        return 1

    _clear_bridge_runtime_state()
    logger.info(f"Bridge stopped (pid={pid}).")
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
        logger.info("`adk` CLI not found. Install with: pip install google-adk")
        return 1

    agent_dir = Path(__file__).parent.resolve()
    cmd = ["adk", "run", str(agent_dir), *passthrough_args]
    return subprocess.call(cmd)


def _cmd_onboard(force: bool) -> int:
    config_path = get_config_path()
    existed = config_path.exists()

    if force or not existed:
        config = default_config()
        saved_to = save_config(config, config_path=config_path)
        state = "reset to defaults" if force and existed else "created"
    else:
        # Refresh while preserving existing values.
        config = load_config(config_path=config_path)
        saved_to = save_config(config, config_path=config_path)
        state = "refreshed"

    workspace = Path(str(config.get("agent", {}).get("workspace", ""))).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "skills").mkdir(parents=True, exist_ok=True)

    print(f"Config {state}: {saved_to}")
    print(f"Workspace ready: {workspace}")
    print("Next steps:")
    print(f"1. Edit config: {saved_to}")
    print("2. Configure providers/channels/web sections and their `enabled` flags")
    print("3. Fill providers.<provider>.apiKey for the enabled provider (and channel credentials if needed)")
    print("4. Start gateway: openheron gateway")
    print("5. Dry run: openheron doctor")
    return 0


def _cmd_gateway_local(sender_id: str, chat_id: str) -> int:
    return _cmd_gateway(channels="local", sender_id=sender_id, chat_id=chat_id, interactive_local=True)


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
    from .bus.queue import MessageBus
    from .gateway import Gateway

    async def _run() -> int:
        bus = MessageBus()
        names = parse_enabled_channels(channels)
        issues = validate_channel_setup(names)
        if issues:
            for item in issues:
                logger.info(f"[doctor] {item}")
            return 1
        if "whatsapp" in names and _whatsapp_bridge_precheck_enabled():
            bridge_issue = _check_whatsapp_bridge_ready()
            if bridge_issue:
                logger.info(f"[doctor] {bridge_issue}")
                return 1
        mcp_issues = await _required_mcp_preflight(list(getattr(root_agent, "tools", [])))
        if mcp_issues:
            blocking_issues = [item for item in mcp_issues if not _is_non_blocking_mcp_issue(item)]
            non_blocking_issues = [item for item in mcp_issues if _is_non_blocking_mcp_issue(item)]
            for item in non_blocking_issues:
                logger.warning(f"[mcp] {item}; marked unavailable, gateway will continue without this MCP toolset")
            if blocking_issues:
                for item in blocking_issues:
                    logger.info(f"[doctor] {item}")
                return 1

        manager, local_channel = build_channel_manager(
            bus=bus,
            channel_names=names,
            local_writer=logger.info,
        )
        _log_mcp_startup_summary(list(getattr(root_agent, "tools", [])))
        gateway = Gateway(
            agent=root_agent,
            app_name=root_agent.name,
            bus=bus,
            channel_manager=manager,
        )
        await gateway.start()
        logger.info(f"gateway started with channels: {', '.join(names)}")
        if interactive_local and local_channel:
            logger.info("local interactive mode: type /quit or /exit to stop.")
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
        logger.info(f"Error running gateway: {exc}")
        return 1


def _log_mcp_startup_summary(agent_tools: list[object]) -> None:
    """Print a compact MCP summary at gateway startup."""
    summaries = summarize_mcp_toolsets(agent_tools)
    if not summaries:
        logger.info("MCP toolsets: none configured")
        return
    logger.info(f"MCP toolsets: {len(summaries)} server(s) configured")
    for item in summaries:
        status = str(item.get("status", "unknown"))
        status_message = str(item.get("status_message", "")).strip()
        status_suffix = f", status={status}"
        if status == "unavailable" and status_message:
            status_suffix = f"{status_suffix}, reason={status_message}"
        logger.info(
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
        logger.info(f"Error running agent: {exc}")
        return 1

    if not final_text:
        logger.info("(no response)")
        return 0
    logger.info(final_text)
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
        logger.info("Error: --tz can only be used with --cron")
        return 1
    if deliver and not to:
        logger.info("Error: --to is required when --deliver is set")
        return 1

    parsed, parse_error = parse_schedule_input(
        every_seconds=every,
        cron_expr=cron_expr,
        at=at,
        tz=tz,
    )
    if parse_error:
        logger.info(f"Error: {parse_error}")
        return 1
    if parsed is None:  # pragma: no cover - defensive fallback
        logger.info("Error: failed to parse schedule")
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
    logger.info(f"Added job '{job.name}' ({job.id})")
    return 0


def _cmd_cron_remove(job_id: str) -> int:
    if _cron_service().remove_job(job_id):
        logger.info(f"Removed job {job_id}")
        return 0
    logger.info(f"Job {job_id} not found")
    return 1


def _cmd_cron_enable(job_id: str, *, disable: bool) -> int:
    job = _cron_service().enable_job(job_id, enabled=not disable)
    if job is None:
        logger.info(f"Job {job_id} not found")
        return 1
    state = "disabled" if disable else "enabled"
    logger.info(f"Job '{job.name}' {state}")
    return 0


def _cmd_cron_run(job_id: str, *, force: bool) -> int:
    async def _run():
        return await _cron_service().run_job_with_result(job_id, force=force)

    result = asyncio.run(_run())
    if result.reason == "ok":
        logger.info("Job executed")
        return 0
    if result.reason == "disabled":
        logger.info(f"Job {job_id} is disabled. Use --force to run it once.")
        return 1
    if result.reason == "not_found":
        logger.info(f"Job {job_id} not found")
        return 1
    if result.reason == "no_callback":
        logger.info(
            "Job skipped: no executor callback is configured in this process. "
            "Run via gateway runtime to execute the agent task."
        )
        return 1
    if result.reason == "error":
        if result.error:
            logger.info(f"Job execution failed: {result.error}")
        else:
            logger.info(f"Job execution failed: {job_id}")
        return 1
    logger.info(f"Job skipped: {result.reason}")
    return 1


def _cmd_cron_status() -> int:
    info = _cron_service().status()
    runtime_pid = info.get("runtime_pid")
    runtime_pid_text = str(runtime_pid) if runtime_pid is not None else "-"
    logger.info(
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
    onboard_parser = subparsers.add_parser(
        "onboard",
        help="Initialize ~/.openheron/config.json and workspace.",
    )
    onboard_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing config with defaults.",
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
        help="Run gateway using env/CLI channels (e.g. feishu).",
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

    args = parser.parse_args(argv)
    if args.command != "onboard":
        bootstrap_env_from_config()

    # Global `-m/--message` is single-turn mode only when no subcommand is used.
    if args.command is None and args.message:
        sid = args.session_id or uuid.uuid4().hex[:12]
        code = _cmd_message(args.message, user_id=args.user_id, session_id=sid)
    elif args.command == "cron":
        code = _dispatch_cron_command(args, parser)
    elif args.command == "channels":
        code = _dispatch_channels_command(args, parser)
    elif args.command == "provider":
        code = _dispatch_provider_command(args, parser)
    else:
        handlers: dict[str, Callable[[], int]] = {
            "onboard": lambda: _cmd_onboard(force=args.force),
            "skills": _cmd_skills,
            "mcps": _cmd_mcps,
            "spawn": _cmd_spawn,
            "doctor": lambda: _cmd_doctor(output_json=args.output_json, verbose=args.verbose),
            "run": lambda: _cmd_run(args.adk_args),
            "gateway-local": lambda: _cmd_gateway_local(sender_id=args.sender_id, chat_id=args.chat_id),
            "gateway": lambda: _cmd_gateway(
                channels=args.channels,
                sender_id=args.sender_id,
                chat_id=args.chat_id,
                interactive_local=args.interactive_local,
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
