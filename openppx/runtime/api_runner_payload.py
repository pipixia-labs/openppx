"""Shared payload loading for declarative API runners."""

from __future__ import annotations

import json
import os
import sys
from typing import Any


PAYLOAD_JSON_ENV = "OPENPPX_API_RUNNER_PAYLOAD_JSON"
PAYLOAD_STDIN_ENV = "OPENPPX_API_RUNNER_PAYLOAD_STDIN"
ARGS_JSON_ENV = "OPENPPX_SKILL_ARGS_JSON"


def load_api_runner_payload() -> dict[str, Any] | None:
    """Load a combined runner payload from env or stdin when configured."""
    raw = os.getenv(PAYLOAD_JSON_ENV, "").strip()
    if raw:
        return _parse_payload(raw)
    if not _payload_stdin_enabled():
        return None
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("OPENPPX_API_RUNNER_PAYLOAD_STDIN was set but stdin payload is empty")
    return _parse_payload(raw)


def load_recipe_from_payload_or_env(
    *,
    payload: dict[str, Any] | None,
    env_var: str,
    runner_name: str,
) -> dict[str, Any]:
    """Load one runner recipe from a combined payload or legacy env var."""
    if payload is not None:
        recipe = payload.get("recipe")
        if not isinstance(recipe, dict):
            raise ValueError(f"{runner_name} API runner payload recipe must be a JSON object")
        return recipe

    raw = os.getenv(env_var, "").strip()
    if not raw:
        raise ValueError(f"{env_var} is required")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{runner_name} API recipe must be a JSON object")
    return parsed


def load_args_from_payload_or_env(payload: dict[str, Any] | None) -> Any:
    """Load API args from a combined payload or legacy env var."""
    if payload is not None:
        return payload.get("args", {})

    raw = os.getenv(ARGS_JSON_ENV, "").strip()
    if not raw:
        return {}
    return json.loads(raw)


def build_api_runner_payload_json(*, recipe: dict[str, Any], args: Any) -> str:
    """Serialize a combined API runner payload."""
    return json.dumps({"recipe": recipe, "args": {} if args is None else args}, ensure_ascii=False, default=str)


def _parse_payload(raw: str) -> dict[str, Any]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("OpenPPX API runner payload must be a JSON object")
    return parsed


def _payload_stdin_enabled() -> bool:
    return os.getenv(PAYLOAD_STDIN_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
