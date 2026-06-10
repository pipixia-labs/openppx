"""Versioned checkpoint helpers for GUI task runner state."""

from __future__ import annotations

from typing import Any


GUI_TASK_CHECKPOINT_SCHEMA = "openppx.gui_task_checkpoint"
GUI_TASK_CHECKPOINT_SCHEMA_VERSION = 1


def normalize_gui_task_checkpoint(
    raw: dict[str, Any] | None,
    *,
    task: str = "",
    max_steps: int | None = None,
    dry_run: bool | None = None,
    current_plan: str = "",
    status_code: str = "running",
    summary: str = "",
    include_schema: bool = True,
) -> dict[str, Any]:
    """Return a mutable GUI task checkpoint compatible with schema v1.

    Legacy checkpoints without schema metadata are accepted. The returned
    payload keeps the runner state at the top level so old consumers that read
    ``task`` / ``history`` / ``next_step`` continue to work.
    """
    state = dict(raw or {})
    _validate_gui_task_checkpoint_version(state)
    normalized_task = _text(state.get("task")) or _text(task)
    normalized_plan = _text(state.get("current_plan")) or _text(current_plan) or normalized_task
    history = normalize_gui_history(state.get("history"))
    saved_info = normalize_gui_saved_info(state.get("saved_info"))
    resolved_next_step = _positive_int(state.get("next_step"))
    if resolved_next_step is None:
        resolved_next_step = len(history) + 1

    payload: dict[str, Any] = dict(state)
    if include_schema:
        payload["schema"] = GUI_TASK_CHECKPOINT_SCHEMA
        payload["schema_version"] = GUI_TASK_CHECKPOINT_SCHEMA_VERSION
    payload["task"] = normalized_task
    payload["max_steps"] = _maybe_int(state.get("max_steps"), default=max_steps)
    payload["dry_run"] = _bool(state.get("dry_run"), default=bool(dry_run) if dry_run is not None else False)
    payload["current_plan"] = normalized_plan
    payload["saved_info"] = saved_info
    payload["history"] = history
    payload["next_step"] = resolved_next_step
    payload["status_code"] = _text(state.get("status_code")) or _text(status_code) or "running"
    payload["summary"] = _text(state.get("summary")) or _text(summary) or _default_summary(resolved_next_step)
    return payload


def build_gui_task_checkpoint(
    *,
    task: str,
    max_steps: int | None,
    dry_run: bool,
    current_plan: str,
    saved_info: dict[str, str],
    history: list[dict[str, Any]],
    next_step: int,
    status_code: str = "running",
    summary: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a schema-versioned GUI task checkpoint payload."""
    payload = {
        "task": task,
        "max_steps": max_steps,
        "dry_run": dry_run,
        "current_plan": current_plan,
        "saved_info": dict(saved_info),
        "history": [dict(item) for item in history],
        "next_step": next_step,
        "status_code": status_code,
        "summary": summary or _default_summary(next_step),
    }
    if extra:
        payload.update(extra)
    return normalize_gui_task_checkpoint(payload, include_schema=True)


def normalize_gui_history(value: Any) -> list[dict[str, Any]]:
    """Return a safe mutable history list from checkpoint payload."""
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def normalize_gui_saved_info(value: Any) -> dict[str, str]:
    """Return a string map from checkpoint payload."""
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _default_summary(next_step: int) -> str:
    return f"GUI task checkpoint before step {next_step}."


def _validate_gui_task_checkpoint_version(state: dict[str, Any]) -> None:
    """Reject openppx GUI checkpoint payloads from unsupported schemas."""
    schema = _text(state.get("schema"))
    if schema and schema != GUI_TASK_CHECKPOINT_SCHEMA:
        raise ValueError(f"unsupported GUI task checkpoint schema {schema!r}")
    raw_version = state.get("schema_version")
    if raw_version in (None, ""):
        return
    version = _maybe_int(raw_version, default=None)
    if version != GUI_TASK_CHECKPOINT_SCHEMA_VERSION:
        raise ValueError(f"unsupported GUI task checkpoint schema_version {raw_version!r}")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _maybe_int(value: Any, *, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except Exception:
        return default


def _positive_int(value: Any) -> int | None:
    parsed = _maybe_int(value, default=None)
    if parsed is None:
        return None
    return parsed if parsed > 0 else None


def _bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


__all__ = [
    "GUI_TASK_CHECKPOINT_SCHEMA",
    "GUI_TASK_CHECKPOINT_SCHEMA_VERSION",
    "build_gui_task_checkpoint",
    "normalize_gui_history",
    "normalize_gui_saved_info",
    "normalize_gui_task_checkpoint",
]
