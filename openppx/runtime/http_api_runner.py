"""Narrow subprocess runner for declarative skill HTTP APIs."""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_RESPONSE_MAX_BYTES = 512 * 1024
_TEMPLATE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


def main() -> int:
    """Execute one HTTP API recipe and print a JSON result."""
    try:
        recipe = _load_recipe()
        args = _load_args()
        request = _build_request(recipe, args=args)
        timeout = _timeout_seconds(recipe)
        max_bytes = _response_max_bytes(recipe)
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - skill recipes are explicit user/project config.
            body, truncated = _read_body(response, max_bytes=max_bytes)
            _emit_response(
                ok=200 <= int(response.status) < 300,
                status_code=int(response.status),
                headers=dict(response.headers.items()),
                body=body,
                truncated=truncated,
            )
            return 0 if 200 <= int(response.status) < 300 else 1
    except HTTPError as exc:
        body, truncated = _read_body(exc, max_bytes=_response_max_bytes(_safe_recipe()))
        _emit_response(
            ok=False,
            status_code=int(exc.code),
            headers=dict(exc.headers.items()),
            body=body,
            truncated=truncated,
            error=f"HTTP {exc.code}",
        )
        return 1 if _fail_on_http_error(_safe_recipe()) else 0
    except (TimeoutError, URLError, OSError, ValueError) as exc:
        _emit_response(ok=False, status_code=None, headers={}, body="", truncated=False, error=str(exc))
        return 1


def _load_recipe() -> dict[str, Any]:
    raw = os.getenv("OPENPPX_HTTP_API_RECIPE_JSON", "").strip()
    if not raw:
        raise ValueError("OPENPPX_HTTP_API_RECIPE_JSON is required")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("HTTP API recipe must be a JSON object")
    return parsed


def _safe_recipe() -> dict[str, Any]:
    try:
        return _load_recipe()
    except Exception:
        return {}


def _load_args() -> Any:
    raw = os.getenv("OPENPPX_SKILL_ARGS_JSON", "").strip()
    if not raw:
        return {}
    return json.loads(raw)


def _build_request(recipe: dict[str, Any], *, args: Any) -> Request:
    method = str(recipe.get("method", "GET") or "GET").strip().upper()
    url = _render_string(str(recipe.get("url", "")), args=args).strip()
    url = _append_query(url, recipe.get("query"), args=args)
    _validate_url(url)
    headers = _render_headers(recipe.get("headers"), args=args)
    body = _request_body(recipe, headers=headers, args=args)
    return Request(url, data=body, headers=headers, method=method)


def _append_query(url: str, query: Any, *, args: Any) -> str:
    if not query:
        return url
    if not isinstance(query, dict):
        raise ValueError("HTTP API recipe query must be a JSON object")
    rendered = {str(key): _render_value(value, args=args) for key, value in query.items()}
    split = urlsplit(url)
    appended = urlencode(rendered, doseq=True)
    existing = split.query
    merged = f"{existing}&{appended}" if existing else appended
    return urlunsplit((split.scheme, split.netloc, split.path, merged, split.fragment))


def _validate_url(url: str) -> None:
    split = urlsplit(url)
    if split.scheme not in {"http", "https"}:
        raise ValueError("HTTP API recipe url must use http or https")
    if not split.netloc:
        raise ValueError("HTTP API recipe url must include a host")


def _render_headers(headers: Any, *, args: Any) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise ValueError("HTTP API recipe headers must be a JSON object")
    return {str(key): str(_render_value(value, args=args)) for key, value in headers.items()}


def _request_body(recipe: dict[str, Any], *, headers: dict[str, str], args: Any) -> bytes | None:
    if "json" in recipe or "body_json" in recipe:
        body_value = recipe.get("json") if "json" in recipe else recipe.get("body_json")
        rendered = _render_value(body_value, args=args)
        _set_default_header(headers, "Content-Type", "application/json")
        return json.dumps(rendered, ensure_ascii=False).encode("utf-8")
    if bool(recipe.get("body_from_args", False)):
        _set_default_header(headers, "Content-Type", "application/json")
        return json.dumps(args, ensure_ascii=False).encode("utf-8")
    if "body" in recipe:
        body = _render_string(str(recipe.get("body", "")), args=args)
        return body.encode("utf-8")
    return None


def _set_default_header(headers: dict[str, str], name: str, value: str) -> None:
    lowered = {key.lower() for key in headers}
    if name.lower() not in lowered:
        headers[name] = value


def _render_value(value: Any, *, args: Any) -> Any:
    if isinstance(value, str):
        return _render_string(value, args=args)
    if isinstance(value, list):
        return [_render_value(item, args=args) for item in value]
    if isinstance(value, dict):
        return {str(key): _render_value(item, args=args) for key, item in value.items()}
    return value


def _render_string(template: str, *, args: Any) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key.startswith("args."):
            key = key[5:]
        return str(_lookup_arg(args, key))

    return _TEMPLATE_RE.sub(replace, template)


def _lookup_arg(args: Any, path: str) -> Any:
    current = args
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        raise ValueError(f"missing argument for template placeholder {{{path}}}")
    return current


def _timeout_seconds(recipe: dict[str, Any]) -> float:
    raw = recipe.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    max_value = _env_float("OPENPPX_HTTP_API_TIMEOUT_MAX_SECONDS", 3600.0)
    return max(0.001, min(value, max_value))


def _response_max_bytes(recipe: dict[str, Any]) -> int:
    raw = recipe.get("response_max_bytes", DEFAULT_RESPONSE_MAX_BYTES)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_RESPONSE_MAX_BYTES
    return max(1, min(value, _env_int("OPENPPX_HTTP_API_RESPONSE_MAX_BYTES", DEFAULT_RESPONSE_MAX_BYTES)))


def _fail_on_http_error(recipe: dict[str, Any]) -> bool:
    return bool(recipe.get("fail_on_http_error", True))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except ValueError:
        return default


def _read_body(response: Any, *, max_bytes: int) -> tuple[str, bool]:
    raw = response.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    charset = response.headers.get_content_charset() if response.headers else None
    return raw.decode(charset or "utf-8", errors="replace"), truncated


def _emit_response(
    *,
    ok: bool,
    status_code: int | None,
    headers: dict[str, str],
    body: str,
    truncated: bool,
    error: str = "",
) -> None:
    payload: dict[str, Any] = {
        "ok": ok,
        "status_code": status_code,
        "headers": headers,
        "body": body,
        "truncated": truncated,
    }
    if error:
        payload["error"] = error
    print(json.dumps(payload, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
