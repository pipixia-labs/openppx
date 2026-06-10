"""Explicit browser remote job protocol support.

The protocol is intentionally opt-in. A remote browser provider must declare
job status/output/cancel paths before openppx treats a remote job as live
controllable instead of observation-only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .checkpoint_schema import (
    CheckpointSchemaRegistry,
    DEFAULT_CHECKPOINT_SCHEMA_REGISTRY,
    normalize_task_checkpoint_payload,
)
from .checkpoint_migration_catalog import ensure_default_checkpoint_migration_catalog_applied
from .mcp_job_protocol import extract_path


DEFAULT_BROWSER_REMOTE_JOB_TIMEOUT_MS = 5_000
MAX_BROWSER_REMOTE_JOB_TIMEOUT_MS = 60_000


@dataclass(frozen=True, slots=True)
class BrowserRemoteJobProtocolConfig:
    """Explicit HTTP protocol for remote browser job controls."""

    enabled: bool
    status_path: str
    status_method: str
    status_result_path: str
    output_path: str
    output_method: str
    output_result_path: str
    cancel_path: str
    cancel_method: str
    cancel_result_path: str
    pause_path: str
    pause_method: str
    pause_result_path: str
    resume_path: str
    resume_method: str
    resume_result_path: str
    checkpoint_path: str
    checkpoint_schema: str
    checkpoint_schema_version: int | None
    poll_timeout_ms: int

    @property
    def runner_capabilities(self) -> dict[str, bool]:
        """Return conservative TaskRun capabilities implied by this protocol."""
        return {
            "status": bool(self.status_path),
            "cancel": bool(self.cancel_path),
            "interrupt": False,
            "output": bool(self.output_path),
            "artifact": False,
            "rejoin": bool(self.status_path),
            "pause": bool(self.pause_path),
            "checkpoint": bool(self.resume_path or self.checkpoint_path),
            "resume": bool(self.resume_path),
        }

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable protocol payload."""
        payload = {
            "enabled": self.enabled,
            "status_path": self.status_path,
            "status_method": self.status_method,
            "status_result_path": self.status_result_path,
            "output_path": self.output_path,
            "output_method": self.output_method,
            "output_result_path": self.output_result_path,
            "cancel_path": self.cancel_path,
            "cancel_method": self.cancel_method,
            "cancel_result_path": self.cancel_result_path,
            "pause_path": self.pause_path,
            "pause_method": self.pause_method,
            "pause_result_path": self.pause_result_path,
            "resume_path": self.resume_path,
            "resume_method": self.resume_method,
            "resume_result_path": self.resume_result_path,
            "checkpoint_path": self.checkpoint_path,
            "poll_timeout_ms": self.poll_timeout_ms,
        }
        if self.checkpoint_schema:
            payload["checkpoint_schema"] = self.checkpoint_schema
        if self.checkpoint_schema_version is not None:
            payload["checkpoint_schema_version"] = self.checkpoint_schema_version
        return payload


@dataclass(frozen=True, slots=True)
class BrowserRemoteJobCallResult:
    """Result of invoking one remote browser job protocol endpoint."""

    ok: bool
    payload: Any = None
    raw_payload: Any = None
    error: str = ""
    missing_endpoint: bool = False
    status_code: int | None = None


def browser_remote_job_protocol_from_payload(raw: Any) -> BrowserRemoteJobProtocolConfig | None:
    """Parse a browser remote job protocol declaration."""
    if not isinstance(raw, dict):
        return None
    enabled = _bool(_pick(raw, "enabled", "enabled", True), default=True)
    if not enabled:
        return None
    status_path = _text(_pick(raw, "status_path", "statusPath", ""))
    output_path = _text(_pick(raw, "output_path", "outputPath", ""))
    cancel_path = _text(_pick(raw, "cancel_path", "cancelPath", ""))
    pause_path = _text(_pick(raw, "pause_path", "pausePath", ""))
    resume_path = _text(_pick(raw, "resume_path", "resumePath", ""))
    checkpoint_path = _text(_pick(raw, "checkpoint_path", "checkpointPath", ""))
    if not status_path and not output_path and not cancel_path and not pause_path and not resume_path and not checkpoint_path:
        return None
    checkpoint_schema_version = _optional_int(
        _pick(raw, "checkpoint_schema_version", "checkpointSchemaVersion", None)
    )
    return BrowserRemoteJobProtocolConfig(
        enabled=True,
        status_path=status_path,
        status_method=_normalize_method(_pick(raw, "status_method", "statusMethod", "GET"), default="GET"),
        status_result_path=_text(_pick(raw, "status_result_path", "statusResultPath", "")),
        output_path=output_path,
        output_method=_normalize_method(_pick(raw, "output_method", "outputMethod", "GET"), default="GET"),
        output_result_path=_text(_pick(raw, "output_result_path", "outputResultPath", "")),
        cancel_path=cancel_path,
        cancel_method=_normalize_method(_pick(raw, "cancel_method", "cancelMethod", "POST"), default="POST"),
        cancel_result_path=_text(_pick(raw, "cancel_result_path", "cancelResultPath", "")),
        pause_path=pause_path,
        pause_method=_normalize_method(_pick(raw, "pause_method", "pauseMethod", "POST"), default="POST"),
        pause_result_path=_text(_pick(raw, "pause_result_path", "pauseResultPath", "")),
        resume_path=resume_path,
        resume_method=_normalize_method(_pick(raw, "resume_method", "resumeMethod", "POST"), default="POST"),
        resume_result_path=_text(_pick(raw, "resume_result_path", "resumeResultPath", "")),
        checkpoint_path=checkpoint_path,
        checkpoint_schema=_text(_pick(raw, "checkpoint_schema", "checkpointSchema", "")),
        checkpoint_schema_version=checkpoint_schema_version,
        poll_timeout_ms=_normalize_timeout_ms(_pick(raw, "poll_timeout_ms", "pollTimeoutMs", None)),
    )


def call_browser_remote_job_status(
    *,
    proxy_url: str,
    protocol: BrowserRemoteJobProtocolConfig,
    job_id: str,
    token: str = "",
    context_payload: dict[str, Any] | None = None,
) -> BrowserRemoteJobCallResult:
    """Call the configured browser remote job status endpoint."""
    if not protocol.status_path:
        return BrowserRemoteJobCallResult(
            ok=False,
            error="Browser remote job status endpoint is not configured.",
            missing_endpoint=True,
        )
    return _call_browser_remote_job_endpoint(
        proxy_url=proxy_url,
        method=protocol.status_method,
        path_template=protocol.status_path,
        result_path=protocol.status_result_path,
        job_id=job_id,
        token=token,
        context_payload=context_payload,
        timeout_ms=protocol.poll_timeout_ms,
    )


def call_browser_remote_job_output(
    *,
    proxy_url: str,
    protocol: BrowserRemoteJobProtocolConfig,
    job_id: str,
    token: str = "",
    context_payload: dict[str, Any] | None = None,
) -> BrowserRemoteJobCallResult:
    """Call the configured browser remote job output endpoint."""
    if not protocol.output_path:
        return BrowserRemoteJobCallResult(
            ok=False,
            error="Browser remote job output endpoint is not configured.",
            missing_endpoint=True,
        )
    return _call_browser_remote_job_endpoint(
        proxy_url=proxy_url,
        method=protocol.output_method,
        path_template=protocol.output_path,
        result_path=protocol.output_result_path,
        job_id=job_id,
        token=token,
        context_payload=context_payload,
        timeout_ms=protocol.poll_timeout_ms,
    )


def call_browser_remote_job_cancel(
    *,
    proxy_url: str,
    protocol: BrowserRemoteJobProtocolConfig,
    job_id: str,
    token: str = "",
    context_payload: dict[str, Any] | None = None,
) -> BrowserRemoteJobCallResult:
    """Call the configured browser remote job cancel endpoint."""
    if not protocol.cancel_path:
        return BrowserRemoteJobCallResult(
            ok=False,
            error="Browser remote job cancel endpoint is not configured.",
            missing_endpoint=True,
        )
    return _call_browser_remote_job_endpoint(
        proxy_url=proxy_url,
        method=protocol.cancel_method,
        path_template=protocol.cancel_path,
        result_path=protocol.cancel_result_path,
        job_id=job_id,
        token=token,
        context_payload=context_payload,
        timeout_ms=protocol.poll_timeout_ms,
    )


def call_browser_remote_job_pause(
    *,
    proxy_url: str,
    protocol: BrowserRemoteJobProtocolConfig,
    job_id: str,
    token: str = "",
    context_payload: dict[str, Any] | None = None,
) -> BrowserRemoteJobCallResult:
    """Call the configured browser remote job pause endpoint."""
    if not protocol.pause_path:
        return BrowserRemoteJobCallResult(
            ok=False,
            error="Browser remote job pause endpoint is not configured.",
            missing_endpoint=True,
        )
    return _call_browser_remote_job_endpoint(
        proxy_url=proxy_url,
        method=protocol.pause_method,
        path_template=protocol.pause_path,
        result_path=protocol.pause_result_path,
        job_id=job_id,
        token=token,
        context_payload=context_payload,
        timeout_ms=protocol.poll_timeout_ms,
    )


def call_browser_remote_job_resume(
    *,
    proxy_url: str,
    protocol: BrowserRemoteJobProtocolConfig,
    job_id: str,
    token: str = "",
    context_payload: dict[str, Any] | None = None,
    checkpoint_payload: dict[str, Any] | None = None,
) -> BrowserRemoteJobCallResult:
    """Call the configured browser remote job resume endpoint."""
    if not protocol.resume_path:
        return BrowserRemoteJobCallResult(
            ok=False,
            error="Browser remote job resume endpoint is not configured.",
            missing_endpoint=True,
        )
    request_context = dict(context_payload or {})
    if checkpoint_payload:
        request_context["checkpoint"] = checkpoint_payload
    return _call_browser_remote_job_endpoint(
        proxy_url=proxy_url,
        method=protocol.resume_method,
        path_template=protocol.resume_path,
        result_path=protocol.resume_result_path,
        job_id=job_id,
        token=token,
        context_payload=request_context,
        timeout_ms=protocol.poll_timeout_ms,
    )


def normalize_browser_remote_job_snapshot(payload: Any, *, default_status: str = "running") -> dict[str, Any]:
    """Return a TaskRun-compatible status snapshot from provider payload."""
    if isinstance(payload, dict):
        snapshot = dict(payload)
    else:
        snapshot = {"output": _render_payload(payload)}
    if "status" not in snapshot:
        for alias in ("jobStatus", "job_status", "state", "phase"):
            if alias in snapshot:
                snapshot["status"] = snapshot[alias]
                break
    if "status" not in snapshot and default_status:
        snapshot["status"] = default_status
    return snapshot


def normalize_browser_remote_job_checkpoint_payload(
    *,
    protocol: BrowserRemoteJobProtocolConfig,
    payload: dict[str, Any],
    registry: CheckpointSchemaRegistry | None = None,
) -> dict[str, Any]:
    """Normalize a provider checkpoint using the declared browser job protocol."""
    normalized = dict(payload)
    if "schema_version" not in normalized and "schemaVersion" in normalized:
        parsed_alias_version = _optional_int(normalized.get("schemaVersion"))
        if parsed_alias_version is not None:
            normalized["schema_version"] = parsed_alias_version
    declared_schema = _text(protocol.checkpoint_schema)
    if declared_schema:
        existing_schema = _text(normalized.get("schema"))
        if existing_schema and existing_schema != declared_schema:
            raise ValueError(
                f"Browser remote checkpoint schema mismatch: expected {declared_schema!r}, got {existing_schema!r}"
            )
        normalized["schema"] = declared_schema
    declared_version = protocol.checkpoint_schema_version
    if declared_version is not None:
        existing_version = _optional_int(normalized.get("schema_version"))
        if existing_version is not None and existing_version != declared_version:
            if existing_version > declared_version:
                raise ValueError(
                    "Browser remote checkpoint schema_version mismatch: "
                    f"expected {declared_version!r}, got {normalized.get('schema_version')!r}"
                )
            resolved_registry = registry or DEFAULT_CHECKPOINT_SCHEMA_REGISTRY
            if not declared_schema:
                raise ValueError(
                    "Browser remote checkpoint schema_version migration requires checkpointSchema."
                )
            if registry is None:
                ensure_default_checkpoint_migration_catalog_applied()
            if resolved_registry.resolve(
                runner_name="browser_remote",
                checkpoint_type="browser_remote_job_state",
                payload_schema=declared_schema,
            ) is None:
                raise ValueError(
                    "Browser remote checkpoint schema_version mismatch and no registered migration spec: "
                    f"expected {declared_version!r}, got {normalized.get('schema_version')!r}"
                )
            return normalize_task_checkpoint_payload(
                runner_name="browser_remote",
                checkpoint_type="browser_remote_job_state",
                payload=normalized,
                registry=resolved_registry,
            )
        normalized["schema_version"] = declared_version
    return normalized


def _call_browser_remote_job_endpoint(
    *,
    proxy_url: str,
    method: str,
    path_template: str,
    result_path: str,
    job_id: str,
    token: str,
    context_payload: dict[str, Any] | None,
    timeout_ms: int,
) -> BrowserRemoteJobCallResult:
    safe_job_id = _text(job_id)
    if not safe_job_id:
        return BrowserRemoteJobCallResult(ok=False, error="Browser remote job id is required.")
    try:
        path = _render_path(path_template, job_id=safe_job_id, context_payload=context_payload)
        url = _join_url(proxy_url, path)
        body = None
        headers = {"Accept": "application/json"}
        normalized_method = _normalize_method(method, default="GET")
        if normalized_method not in {"GET", "DELETE"}:
            body_payload = {"job_id": safe_job_id, "jobId": safe_job_id}
            if context_payload:
                body_payload["context"] = context_payload
                if isinstance(context_payload.get("checkpoint"), dict):
                    body_payload["checkpoint"] = context_payload["checkpoint"]
            body = json.dumps(body_payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["X-OpenPPX-Browser-Proxy-Token"] = token
        with urlopen(
            Request(url, data=body, headers=headers, method=normalized_method),
            timeout=max(0.1, min(timeout_ms / 1000.0, MAX_BROWSER_REMOTE_JOB_TIMEOUT_MS / 1000.0)),
        ) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
            raw_payload = _parse_json_payload(raw_text)
            extracted = extract_path(raw_payload, result_path) if result_path else raw_payload
            return BrowserRemoteJobCallResult(ok=True, payload=extracted, raw_payload=raw_payload)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raw_payload = _parse_json_payload(detail)
        error = _extract_error_text(raw_payload) or detail.strip() or str(exc)
        return BrowserRemoteJobCallResult(
            ok=False,
            payload=raw_payload,
            raw_payload=raw_payload,
            error=error,
            status_code=exc.code,
        )
    except (TimeoutError, URLError, OSError, ValueError) as exc:
        return BrowserRemoteJobCallResult(ok=False, error=str(exc))


def _render_path(path_template: str, *, job_id: str, context_payload: dict[str, Any] | None) -> str:
    path = _text(path_template)
    if not path:
        raise ValueError("Browser remote job protocol path is required.")
    rendered = path.replace("{job_id}", job_id).replace("{jobId}", job_id)
    context = context_payload or {}
    if "{job_id}" not in path and "{jobId}" not in path:
        separator = "&" if "?" in rendered else "?"
        rendered = f"{rendered}{separator}{urlencode({'job_id': job_id})}"
    if not rendered.startswith("/"):
        rendered = f"/{rendered}"
    _ = context
    return rendered


def _join_url(proxy_url: str, path: str) -> str:
    base = _text(proxy_url).rstrip("/")
    if not base:
        raise ValueError("Browser remote proxy URL is required.")
    return f"{base}{path}"


def _parse_json_payload(raw_text: str) -> Any:
    text = (raw_text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"output": text}


def _render_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, ensure_ascii=False)
    return str(payload or "")


def _extract_error_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("error", "message", "detail"):
        value = _text(payload.get(key))
        if value:
            return value
    return ""


def _normalize_method(value: Any, *, default: str) -> str:
    method = _text(value).upper()
    return method if method in {"GET", "POST", "PUT", "PATCH", "DELETE"} else default


def _normalize_timeout_ms(value: Any) -> int:
    if value is None:
        return DEFAULT_BROWSER_REMOTE_JOB_TIMEOUT_MS
    try:
        parsed = int(value)
    except Exception:
        return DEFAULT_BROWSER_REMOTE_JOB_TIMEOUT_MS
    return max(100, min(parsed, MAX_BROWSER_REMOTE_JOB_TIMEOUT_MS))


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _pick(raw: dict[str, Any], snake: str, camel: str, default: Any) -> Any:
    if snake in raw:
        return raw[snake]
    if camel in raw:
        return raw[camel]
    return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default
