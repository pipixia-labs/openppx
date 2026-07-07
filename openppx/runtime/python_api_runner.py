"""Narrow subprocess runner for declarative skill Python SDK APIs."""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from .api_runner_payload import load_api_runner_payload
    from .api_runner_payload import load_args_from_payload_or_env
    from .api_runner_payload import load_recipe_from_payload_or_env
except ImportError:  # pragma: no cover - supports direct script execution.
    from api_runner_payload import load_api_runner_payload
    from api_runner_payload import load_args_from_payload_or_env
    from api_runner_payload import load_recipe_from_payload_or_env


_TEMPLATE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}")
_FULL_TEMPLATE_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_.-]*)\}$")
_DOTTED_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def main() -> int:
    """Execute one Python API recipe and print a JSON result."""
    try:
        payload = load_api_runner_payload()
        recipe = _load_recipe(payload)
        args_payload = _load_args(payload)
        module_name, function_path = _callable_ref(recipe)
        _validate_dotted_name(module_name, label="module")
        _validate_dotted_name(function_path, label="function")
        sys.path.insert(0, os.getcwd())
        function = _resolve_function(module_name, function_path)
        positional_args, keyword_args = _call_args(recipe, args_payload)
        result = function(*positional_args, **keyword_args)
        _emit_response(ok=True, result=result)
        if bool(recipe.get("fail_on_ok_false", False)) and isinstance(result, dict) and result.get("ok") is False:
            return 1
        return 0
    except Exception as exc:
        _emit_response(ok=False, error=str(exc), error_type=type(exc).__name__)
        return 1


def _load_recipe(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return load_recipe_from_payload_or_env(
        payload=payload,
        env_var="OPENPPX_PYTHON_API_RECIPE_JSON",
        runner_name="Python",
    )


def _load_args(payload: dict[str, Any] | None = None) -> Any:
    return load_args_from_payload_or_env(payload)


def _callable_ref(recipe: dict[str, Any]) -> tuple[str, str]:
    if "callable" in recipe:
        ref = str(recipe.get("callable", "")).strip()
        if ":" not in ref:
            raise ValueError("Python API recipe callable must be module:function")
        module_name, function_path = ref.split(":", 1)
        return module_name.strip(), function_path.strip()
    return str(recipe.get("module", "")).strip(), str(recipe.get("function", "")).strip()


def _validate_dotted_name(value: str, *, label: str) -> None:
    if not _DOTTED_NAME_RE.fullmatch(value):
        raise ValueError(f"Python API recipe {label} must be a dotted Python identifier")


def _resolve_function(module_name: str, function_path: str) -> Callable[..., Any]:
    module = importlib.import_module(module_name)
    _ensure_skill_local_module(module)
    current: Any = module
    for part in function_path.split("."):
        current = getattr(current, part)
    if not callable(current):
        raise ValueError(f"Python API recipe target {module_name}:{function_path} is not callable")
    return current


def _ensure_skill_local_module(module: Any) -> None:
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise ValueError("Python API recipe module must be a file-backed module under the skill root")
    skill_root = Path(os.getcwd()).resolve(strict=False)
    module_path = Path(str(module_file)).resolve(strict=False)
    try:
        module_path.relative_to(skill_root)
    except ValueError as exc:
        raise ValueError("Python API recipe module must resolve under the skill root") from exc


def _call_args(recipe: dict[str, Any], args_payload: Any) -> tuple[list[Any], dict[str, Any]]:
    has_positional = "args" in recipe
    has_keyword = "kwargs" in recipe
    if has_positional:
        raw_args = recipe.get("args")
        if not isinstance(raw_args, list):
            raise ValueError("Python API recipe args must be a JSON array")
        positional_args = [_render_value(value, args=args_payload) for value in raw_args]
    elif not has_keyword and not isinstance(args_payload, dict):
        positional_args = [args_payload]
    else:
        positional_args = []

    if has_keyword:
        raw_kwargs = recipe.get("kwargs")
        if not isinstance(raw_kwargs, dict):
            raise ValueError("Python API recipe kwargs must be a JSON object")
        keyword_args = {str(key): _render_value(value, args=args_payload) for key, value in raw_kwargs.items()}
    elif not has_positional and isinstance(args_payload, dict):
        keyword_args = dict(args_payload)
    else:
        keyword_args = {}
    return positional_args, keyword_args


def _render_value(value: Any, *, args: Any) -> Any:
    if isinstance(value, str):
        return _render_string(value, args=args)
    if isinstance(value, list):
        return [_render_value(item, args=args) for item in value]
    if isinstance(value, dict):
        return {str(key): _render_value(item, args=args) for key, item in value.items()}
    return value


def _render_string(template: str, *, args: Any) -> Any:
    full_match = _FULL_TEMPLATE_RE.fullmatch(template)
    if full_match:
        return _lookup_arg(args, full_match.group(1))

    def replace(match: re.Match[str]) -> str:
        return str(_lookup_arg(args, match.group(1)))

    return _TEMPLATE_RE.sub(replace, template)


def _lookup_arg(args: Any, path: str) -> Any:
    if path == "args":
        return args
    if path.startswith("args."):
        path = path[5:]
    current = args
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        raise ValueError(f"missing argument for template placeholder {{{path}}}")
    return current


def _emit_response(
    *,
    ok: bool,
    result: Any = None,
    error: str = "",
    error_type: str = "",
) -> None:
    payload: dict[str, Any] = {"ok": ok}
    if ok:
        payload["result"] = result
    else:
        payload["error"] = error
        payload["error_type"] = error_type
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
