"""Narrow subprocess runner for declarative command-backed skill APIs."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from openppx.runtime.api_runner_payload import (
    load_api_runner_payload,
    load_args_from_payload_or_env,
    load_recipe_from_payload_or_env,
)
from openppx.runtime.sandbox import (
    RecipeSandboxOptions,
    WorkspaceDockerSandbox,
    build_workspace_docker_sandbox,
    cleanup_docker_sandbox_container,
    resolve_recipe_sandbox_options,
)


_DEFAULT_OUTPUT_MAX_BYTES = 2 * 1024 * 1024
_DEFAULT_SANDBOX_TIMEOUT_CAP_SECONDS = 3600
_TEMPLATE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}")
_FULL_TEMPLATE_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_.-]*)\}$")


def main() -> int:
    """Execute one command API recipe and mirror command output."""
    docker_sandbox: WorkspaceDockerSandbox | None = None
    try:
        payload = load_api_runner_payload()
        recipe = _load_recipe(payload)
        args_payload = _load_args(payload)
        sandbox_options = _recipe_sandbox_options(recipe)
        argv = _render_argv(recipe, args_payload)
        stdin = _render_stdin(recipe, args_payload)
        timeout = _timeout_seconds(recipe)
        output_max_bytes = _output_max_bytes(recipe)
        if sandbox_options is not None:
            docker_sandbox = build_workspace_docker_sandbox(
                command_argv=argv,
                workspace=Path(os.getcwd()),
                cwd=Path(os.getcwd()),
                timeout_seconds=timeout,
                timeout_cap_seconds=_sandbox_timeout_cap_seconds(),
                stdin=stdin,
                env=_render_env(recipe, args_payload, inherit_host_env=False),
                labels={
                    "openppx.tool": "command_api",
                    "openppx.runner": "command_api",
                    **sandbox_options.labels,
                },
                image=sandbox_options.image,
                network_mode=sandbox_options.network_mode,
                network_approved=sandbox_options.network_approved,
            )
            returncode = _run_streaming_command(
                argv=docker_sandbox.argv,
                stdin=docker_sandbox.stdin.decode("utf-8", errors="replace")
                if isinstance(docker_sandbox.stdin, bytes)
                else docker_sandbox.stdin,
                env=os.environ.copy(),
                timeout=docker_sandbox.timeout_seconds,
                output_max_bytes=output_max_bytes,
            )
        else:
            returncode = _run_streaming_command(
                argv=argv,
                stdin=stdin,
                env=_render_env(recipe, args_payload),
                timeout=timeout,
                output_max_bytes=output_max_bytes,
            )
        if returncode != 0 and bool(recipe.get("fail_on_nonzero", True)):
            return int(returncode)
        return 0
    except subprocess.TimeoutExpired as exc:
        if docker_sandbox is not None:
            cleanup_docker_sandbox_container(docker_sandbox.docker_bin, docker_sandbox.container_name)
        _emit_response(ok=False, error=f"command timed out after {exc.timeout} seconds", error_type="TimeoutExpired")
        return 124
    except Exception as exc:
        _emit_response(ok=False, error=str(exc), error_type=type(exc).__name__)
        return 1


def _load_recipe(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return load_recipe_from_payload_or_env(
        payload=payload,
        env_var="OPENPPX_COMMAND_API_RECIPE_JSON",
        runner_name="Command",
    )


def _load_args(payload: dict[str, Any] | None = None) -> Any:
    return load_args_from_payload_or_env(payload)


def _render_argv(recipe: dict[str, Any], args_payload: Any) -> list[str]:
    raw_argv = recipe.get("argv")
    if not isinstance(raw_argv, list) or not raw_argv:
        raise ValueError("Command API recipe argv must be a non-empty JSON array")
    argv = [str(_render_value(item, args=args_payload)) for item in raw_argv]
    if any(not item for item in argv):
        raise ValueError("Command API recipe argv entries must render to non-empty strings")
    return _resolve_executable(argv, allow_system_executable=bool(recipe.get("allow_system_executable", False)))


def _resolve_executable(argv: list[str], *, allow_system_executable: bool) -> list[str]:
    executable = argv[0]
    if allow_system_executable:
        return argv
    executable_path = Path(executable)
    if executable_path.is_absolute() or ".." in executable_path.parts:
        raise ValueError("Command API executable must stay under the skill root unless allow_system_executable is true")
    if len(executable_path.parts) < 2:
        raise ValueError("Command API bare executables require allow_system_executable=true")
    resolved = (Path(os.getcwd()) / executable_path).resolve(strict=False)
    try:
        resolved.relative_to(Path(os.getcwd()).resolve(strict=False))
    except ValueError as exc:
        raise ValueError("Command API executable must resolve under the skill root") from exc
    if not resolved.is_file():
        raise ValueError(f"Command API executable not found: {executable}")
    if not os.access(resolved, os.X_OK):
        raise ValueError(f"Command API executable is not executable: {executable}")
    return [str(resolved), *argv[1:]]


def _render_env(recipe: dict[str, Any], args_payload: Any, *, inherit_host_env: bool = True) -> dict[str, str]:
    env = os.environ.copy() if inherit_host_env else {}
    raw_env = recipe.get("env")
    if raw_env is None:
        return env
    if not isinstance(raw_env, dict):
        raise ValueError("Command API recipe env must be a JSON object")
    for key, value in raw_env.items():
        normalized_key = str(key).strip()
        if not normalized_key:
            raise ValueError("Command API recipe env keys must be non-empty")
        env[normalized_key] = str(_render_value(value, args=args_payload))
    return env


def _render_stdin(recipe: dict[str, Any], args_payload: Any) -> str | None:
    if "stdin" not in recipe:
        return None
    value = _render_value(recipe.get("stdin"), args=args_payload)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _recipe_sandbox_options(recipe: dict[str, Any]) -> RecipeSandboxOptions | None:
    return resolve_recipe_sandbox_options(
        recipe.get("sandbox"),
        runner_name="Command",
        env=os.environ,
        default_backend=os.getenv("OPENPPX_SKILL_API_SANDBOX"),
    )


def _sandbox_timeout_cap_seconds() -> int:
    raw = os.getenv("OPENPPX_SANDBOX_TIMEOUT_MAX_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_SANDBOX_TIMEOUT_CAP_SECONDS
    try:
        value = int(float(raw))
    except Exception:
        return _DEFAULT_SANDBOX_TIMEOUT_CAP_SECONDS
    return max(1, min(value, 24 * 60 * 60))


def _run_streaming_command(
    *,
    argv: list[str],
    stdin: str | None,
    env: dict[str, str],
    timeout: float | None,
    output_max_bytes: int,
) -> int:
    """Run argv without a shell while streaming child output."""
    process = subprocess.Popen(  # noqa: S603 - argv is recipe-validated and shell=False.
        argv,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=os.getcwd(),
        env=env,
    )
    limiter = _OutputLimiter(max_bytes=output_max_bytes)
    pump = threading.Thread(target=_pump_output, args=(process, limiter), daemon=True)
    pump.start()
    if stdin is not None and process.stdin is not None:
        try:
            process.stdin.write(stdin)
            process.stdin.flush()
        finally:
            process.stdin.close()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        pump.join(timeout=1)
        raise
    pump.join(timeout=5)
    return int(returncode)


def _pump_output(process: subprocess.Popen[str], limiter: "_OutputLimiter") -> None:
    stream = process.stdout
    if stream is None:
        return
    try:
        for chunk in iter(stream.readline, ""):
            if not chunk:
                break
            limiter.emit(chunk)
    finally:
        stream.close()


class _OutputLimiter:
    """Thread-safe output byte limiter for streamed command output."""

    def __init__(self, *, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._written_bytes = 0
        self._truncated = False
        self._lock = threading.Lock()

    def emit(self, chunk: str) -> None:
        """Print one output chunk if it fits the configured byte budget."""
        with self._lock:
            if self._truncated:
                return
            encoded = chunk.encode("utf-8", errors="replace")
            remaining = self._max_bytes - self._written_bytes
            if remaining <= 0:
                self._emit_truncation_notice()
                return
            if len(encoded) <= remaining:
                print(chunk, end="", flush=True)
                self._written_bytes += len(encoded)
                return
            clipped = encoded[:remaining]
            while clipped:
                try:
                    text = clipped.decode("utf-8")
                    break
                except UnicodeDecodeError as exc:
                    clipped = clipped[: exc.start]
            else:
                text = ""
            if text:
                print(text, end="", flush=True)
                self._written_bytes += len(text.encode("utf-8", errors="replace"))
            self._emit_truncation_notice()

    def _emit_truncation_notice(self) -> None:
        if self._truncated:
            return
        print(f"\n[openppx: command output truncated to {self._max_bytes} bytes]", flush=True)
        self._truncated = True


def _timeout_seconds(recipe: dict[str, Any]) -> float | None:
    if "timeout_seconds" not in recipe:
        return None
    raw = recipe.get("timeout_seconds")
    if raw is None:
        return None
    value = float(raw)
    if value <= 0:
        raise ValueError("Command API recipe timeout_seconds must be positive")
    return min(value, 24 * 60 * 60)


def _output_max_bytes(recipe: dict[str, Any]) -> int:
    raw = recipe.get("output_max_bytes", _DEFAULT_OUTPUT_MAX_BYTES)
    try:
        value = int(raw)
    except Exception:
        return _DEFAULT_OUTPUT_MAX_BYTES
    return max(1024, min(value, 50 * 1024 * 1024))


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

    def replace(match: Any) -> str:
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


def _emit_response(*, ok: bool, error: str, error_type: str) -> None:
    print(
        json.dumps({"ok": ok, "error": error, "error_type": error_type}, ensure_ascii=False),
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
