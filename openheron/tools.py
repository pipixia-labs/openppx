"""Core tools for openheron (except spawn)."""

from __future__ import annotations

import datetime as dt
import asyncio
import json
import os
import re
import shutil
import shlex
import socket
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .browser_runtime import configure_browser_runtime
from .browser_service import BrowserDispatchRequest, get_browser_control_service
from .bus.events import OutboundMessage
from .env_utils import env_enabled
from .exec_policy import command_segments as _policy_command_segments
from .exec_policy import validate_exec_security as _policy_validate_exec_security
from .logging_utils import debug_logging_enabled, emit_debug
from .runtime.cron_helpers import cron_store_path, format_schedule
from .runtime.cron_schedule_parser import parse_schedule_input
from .runtime.cron_service import CronService
from .runtime.process_sessions import get_process_session_manager
from .runtime.tool_context import get_route
from .security import PathGuard, SecurityPolicy, load_security_policy


_OUTBOUND_PUBLISHER: Callable[[OutboundMessage], Awaitable[None]] | None = None
_SUBAGENT_DISPATCHER: Callable[["SubagentSpawnRequest"], None] | None = None


@dataclass(slots=True)
class SubagentSpawnRequest:
    """A background sub-agent task request created by ``spawn_subagent``.

    The request carries enough metadata for the runtime to:
    1. execute the sub-task in a separate session;
    2. resume the paused parent invocation with the same function_call_id; and
    3. deliver completion notifications to the original channel target.
    """

    task_id: str
    prompt: str
    user_id: str
    session_id: str
    invocation_id: str
    function_call_id: str
    channel: str
    chat_id: str
    notify_on_complete: bool = True


def _security_policy() -> SecurityPolicy:
    return load_security_policy()


def _workspace(policy: SecurityPolicy | None = None) -> Path:
    return (policy or _security_policy()).workspace_root


def _resolve_path(path: str, *, base_dir: Path | None = None, policy: SecurityPolicy | None = None) -> Path:
    active = policy or _security_policy()
    guard = PathGuard(active)
    return guard.resolve_path(path, base_dir=base_dir)


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


_READ_DEFAULT_MAX_BYTES = 50 * 1024
_READ_MIN_MAX_BYTES = 1024
_READ_HARD_MAX_BYTES = 512 * 1024


def _resolve_read_max_bytes() -> int:
    """Resolve read output budget from env with safe bounds."""

    raw = os.getenv("OPENHERON_READ_FILE_MAX_BYTES", "").strip()
    if not raw:
        return _READ_DEFAULT_MAX_BYTES
    try:
        parsed = int(raw)
    except ValueError:
        return _READ_DEFAULT_MAX_BYTES
    return max(_READ_MIN_MAX_BYTES, min(parsed, _READ_HARD_MAX_BYTES))


def _format_bytes(value: int) -> str:
    """Format byte sizes for human-readable continuation notices."""

    if value >= 1024 * 1024:
        return f"{(value / (1024 * 1024)):.1f}MB"
    if value >= 1024:
        return f"{round(value / 1024)}KB"
    return f"{value}B"


def _truncate_utf8_text(text: str, *, max_bytes: int) -> str:
    """Trim text to ``max_bytes`` without breaking UTF-8 character boundaries."""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8")
        except UnicodeDecodeError as exc:
            clipped = clipped[: exc.start]
    return ""


def _resolve_read_path(*, path: str | None, file_path: str | None) -> str | None:
    """Return the effective read path from canonical/alias fields."""

    if isinstance(path, str) and path.strip():
        return path
    if isinstance(file_path, str) and file_path.strip():
        return file_path
    return None


def _parse_positive_int(value: Any, *, field: str) -> int | str:
    """Parse a positive integer from tool input or return an error message."""

    if isinstance(value, bool):
        return f"Error: {field} must be a positive integer."
    parsed: Any = value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return f"Error: {field} must be a positive integer."
        try:
            parsed = int(stripped)
        except ValueError:
            return f"Error: {field} must be a positive integer."
    if not isinstance(parsed, int):
        return f"Error: {field} must be a positive integer."
    if parsed <= 0:
        return f"Error: {field} must be a positive integer."
    return parsed


def read_file(
    path: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    file_path: str | None = None,
) -> str:
    """Read a UTF-8 text file with optional line windowing.

    Args:
        path: Absolute or workspace-relative file path.
        offset: Optional 1-based starting line number.
        limit: Optional max number of lines to return.
        file_path: Optional alias of ``path`` for Claude-style tool calls.

    Returns:
        File content on success, otherwise an "Error: ..." message.

    Notes:
        - Path resolution follows security policy (workspace restriction may apply).
        - Intended for text files.
        - When ``offset``/``limit`` is provided, output is line-windowed.
    """
    _debug(
        "tool.read_file.input",
        {"path": path, "file_path": file_path, "offset": offset, "limit": limit},
    )
    try:
        effective_path = _resolve_read_path(path=path, file_path=file_path)
        if not effective_path:
            return _ret("tool.read_file.output", "Error: Missing required parameter: path (path or file_path).")

        offset_value: int | None = None
        if offset is not None:
            parsed_offset = _parse_positive_int(offset, field="offset")
            if isinstance(parsed_offset, str):
                return _ret("tool.read_file.output", parsed_offset)
            offset_value = parsed_offset

        limit_value: int | None = None
        if limit is not None:
            parsed_limit = _parse_positive_int(limit, field="limit")
            if isinstance(parsed_limit, str):
                return _ret("tool.read_file.output", parsed_limit)
            limit_value = parsed_limit

        target = _resolve_path(effective_path)
        if not target.exists():
            return _ret("tool.read_file.output", f"Error: File not found: {effective_path}")
        if not target.is_file():
            return _ret("tool.read_file.output", f"Error: Not a file: {effective_path}")

        start_line = offset_value or 1
        selected: list[str] = []
        has_more = False
        next_offset: int | None = None
        read_max_bytes = _resolve_read_max_bytes()
        selected_bytes = 0
        with target.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line_number < start_line:
                    continue
                if limit_value is not None and len(selected) >= limit_value:
                    has_more = True
                    next_offset = line_number
                    break
                if limit_value is None:
                    line_bytes = len(line.encode("utf-8"))
                    if selected and selected_bytes + line_bytes > read_max_bytes:
                        has_more = True
                        next_offset = line_number
                        break
                    if not selected and line_bytes > read_max_bytes:
                        clipped = _truncate_utf8_text(line, max_bytes=read_max_bytes)
                        selected.append(clipped)
                        selected_bytes = len(clipped.encode("utf-8"))
                        has_more = True
                        next_offset = line_number + 1
                        break
                    selected_bytes += line_bytes
                selected.append(line)
        result = "".join(selected)
        if has_more and next_offset:
            if limit_value is not None:
                end_line = start_line + max(0, len(selected) - 1)
                notice = f"[Showing lines {start_line}-{end_line}. Use offset={next_offset} to continue.]"
            else:
                budget = _format_bytes(read_max_bytes)
                notice = f"[Read output capped at {budget} for this call. Use offset={next_offset} to continue.]"
            result = f"{result}\n\n{notice}" if result else notice
        _debug(
            "tool.read_file.output",
            {
                "path": str(target),
                "chars": len(result),
                "offset": start_line,
                "limit": limit_value,
                "returned_lines": len(selected),
                "has_more": has_more,
                "next_offset": next_offset,
            },
        )
        return result
    except PermissionError as exc:
        return _ret("tool.read_file.output", f"Error: {exc}")
    except Exception as exc:
        return _ret("tool.read_file.output", f"Error reading file: {exc}")


def write_file(path: str, content: str) -> str:
    """Write UTF-8 text to a file (create parent directories if needed).

    Args:
        path: Absolute or workspace-relative file path.
        content: Full file content to write (overwrite mode).

    Returns:
        Success message with byte count, or an "Error: ..." message.
    """
    _debug("tool.write_file.input", {"path": path, "chars": len(content)})
    try:
        target = _resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        result = f"Successfully wrote {len(content)} bytes to {target}"
        _debug("tool.write_file.output", result)
        return result
    except PermissionError as exc:
        return _ret("tool.write_file.output", f"Error: {exc}")
    except Exception as exc:
        return _ret("tool.write_file.output", f"Error writing file: {exc}")


def edit_file(path: str, old_text: str, new_text: str) -> str:
    """Replace exactly one occurrence of text in a file.

    Args:
        path: Absolute or workspace-relative file path.
        old_text: Exact text snippet to locate (case-sensitive).
        new_text: Replacement text.

    Returns:
        Success message, warning when old_text is not unique, or an "Error: ..." message.

    Notes:
        - This tool refuses ambiguous edits when old_text appears multiple times.
    """
    _debug(
        "tool.edit_file.input",
        {"path": path, "old_text_chars": len(old_text), "new_text_chars": len(new_text)},
    )
    try:
        target = _resolve_path(path)
        if not target.exists():
            return _ret("tool.edit_file.output", f"Error: File not found: {path}")
        if not target.is_file():
            return _ret("tool.edit_file.output", f"Error: Not a file: {path}")
        content = target.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            return _ret("tool.edit_file.output", "Error: old_text not found in file. Make sure it matches exactly.")
        if count > 1:
            return _ret(
                "tool.edit_file.output",
                f"Warning: old_text appears {count} times. Please provide more context to make it unique.",
            )
        target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        result = f"Successfully edited {target}"
        _debug("tool.edit_file.output", result)
        return result
    except PermissionError as exc:
        return _ret("tool.edit_file.output", f"Error: {exc}")
    except Exception as exc:
        return _ret("tool.edit_file.output", f"Error editing file: {exc}")


def list_dir(path: str) -> str:
    """List directory entries in a stable, human-readable format.

    Args:
        path: Absolute or workspace-relative directory path.

    Returns:
        One entry per line, prefixed with "[D]" (directory) or "[F]" (file),
        or an "Error: ..." message.
    """
    _debug("tool.list_dir.input", {"path": path})
    try:
        target = _resolve_path(path)
        if not target.exists():
            return _ret("tool.list_dir.output", f"Error: Directory not found: {path}")
        if not target.is_dir():
            return _ret("tool.list_dir.output", f"Error: Not a directory: {path}")
        entries: list[str] = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            kind = "[D]" if child.is_dir() else "[F]"
            entries.append(f"{kind} {child.name}")
        result = "\n".join(entries) if entries else f"Directory {target} is empty"
        _debug("tool.list_dir.output", {"path": str(target), "entries": len(entries)})
        return result
    except PermissionError as exc:
        return _ret("tool.list_dir.output", f"Error: {exc}")
    except Exception as exc:
        return _ret("tool.list_dir.output", f"Error listing directory: {exc}")


_DENY_PATTERNS = [
    r"\brm\s+-[rf]{1,2}\b",
    r"\bdel\s+/[fq]\b",
    r"\brmdir\s+/s\b",
    r"\b(format|mkfs|diskpart)\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd",
    r"\b(shutdown|reboot|poweroff)\b",
    r":\(\)\s*\{.*\};\s*:",
]

_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_SHELL_CONTROL_TOKENS = {"&&", "||", ";", "|"}
_SHELL_REDIRECTION_TOKENS = {">", ">>", "<", "<<"}
_SHELL_BUILTINS = {"export", "cd", "source", ".", "alias", "unalias", "set", "unset"}
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _looks_like_path_token(token: str) -> bool:
    value = token.strip()
    if not value:
        return False
    if _URL_SCHEME_RE.match(value):
        return False
    if value.startswith("--") and "=" in value:
        _, right = value.split("=", 1)
        return _looks_like_path_token(right)
    if value.startswith("-"):
        return False
    if value.startswith(("/", "./", "../", "~")):
        return True
    if _WINDOWS_ABS_RE.match(value):
        return True
    if "/" in value or "\\" in value:
        return True
    return False


def _validate_exec_paths(argv: list[str], cwd: Path, policy: SecurityPolicy) -> str | None:
    if not policy.restrict_to_workspace:
        return None
    guard = PathGuard(policy)
    for token in argv:
        if not _looks_like_path_token(token):
            continue
        candidate = token
        if token.startswith("--") and "=" in token:
            _, candidate = token.split("=", 1)
        try:
            guard.resolve_path(candidate, base_dir=cwd)
        except PermissionError:
            return f"Error: Command blocked by security policy (path outside workspace: {candidate})"
    return None


def _command_segments(command: str, argv: list[str]) -> list[list[str]]:
    """Return command segments split by chain operators (&&/||/;)."""
    return _policy_command_segments(command, argv)


def _validate_exec_paths_for_command(
    command: str,
    argv: list[str],
    cwd: Path,
    policy: SecurityPolicy,
) -> str | None:
    """Validate path tokens for each parsed command segment."""
    for segment_argv in _command_segments(command, argv):
        path_guard_error = _validate_exec_paths(segment_argv, cwd, policy)
        if path_guard_error:
            return path_guard_error
    return None


def _should_use_shell(argv: list[str]) -> bool:
    """Return whether a command likely requires shell semantics."""
    if not argv:
        return False
    first = argv[0]
    if first in _SHELL_BUILTINS:
        return True
    if _ENV_ASSIGNMENT_RE.match(first):
        return True
    for token in argv:
        if token in _SHELL_CONTROL_TOKENS:
            return True
        if token in _SHELL_REDIRECTION_TOKENS:
            return True
        if token.startswith(">") or token.startswith("<"):
            return True
    return False


def _build_shell_argv(command: str) -> list[str] | None:
    """Build a shell argv list for cross-platform command execution."""
    if os.name == "nt":
        comspec = os.getenv("COMSPEC", "").strip() or "cmd.exe"
        return [comspec, "/c", command]

    shell_from_env = os.getenv("SHELL", "").strip()
    if shell_from_env and Path(shell_from_env).name != "fish":
        return [shell_from_env, "-lc", command]

    bash_path = shutil.which("bash")
    if bash_path:
        return [bash_path, "-lc", command]

    sh_path = shutil.which("sh")
    if sh_path:
        return [sh_path, "-lc", command]

    if shell_from_env:
        return [shell_from_env, "-lc", command]
    return None


def _validate_exec_security(command: str, argv: list[str], policy: SecurityPolicy) -> str | None:
    """Validate command against configured exec security mode."""
    return _policy_validate_exec_security(
        command=command,
        argv=argv,
        policy=policy,
        shell_builtins=_SHELL_BUILTINS,
    )


def _format_exec_output(stdout: str, stderr: str, exit_code: int | None) -> str:
    """Format command output using the legacy exec tool shape."""
    parts: list[str] = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"STDERR:\n{stderr}")
    if exit_code not in (None, 0):
        parts.append(f"Exit code: {exit_code}")
    result = "\n".join(parts).strip() or "(no output)"
    max_len = 12_000
    if len(result) > max_len:
        result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"
    return result


_PROCESS_KEY_TOKENS = {
    "enter": "\r",
    "return": "\r",
    "tab": "\t",
    "space": " ",
    "esc": "\x1b",
    "escape": "\x1b",
    "backspace": "\x7f",
    "delete": "\x1b[3~",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pgup": "\x1b[5~",
    "pageup": "\x1b[5~",
    "pgdn": "\x1b[6~",
    "pagedown": "\x1b[6~",
}

_PROCESS_DEFAULT_LOG_TAIL_LINES = 200
_PROCESS_MAX_LOG_LIMIT = 5000


def _encode_process_keys(keys: list[str] | None) -> tuple[str, list[str]]:
    """Encode tmux-like key tokens into a writable text payload."""

    if not keys:
        return "", []

    payload_parts: list[str] = []
    warnings: list[str] = []
    ctrl_pattern = re.compile(r"^(?:c-|ctrl[+])([a-z])$", flags=re.I)

    for raw in keys:
        token = (raw or "").strip()
        if not token:
            continue
        normalized = token.lower()
        ctrl_match = ctrl_pattern.match(normalized)
        if ctrl_match:
            letter = ctrl_match.group(1)
            payload_parts.append(chr(ord(letter.upper()) - ord("A") + 1))
            continue
        mapped = _PROCESS_KEY_TOKENS.get(normalized)
        if mapped is not None:
            payload_parts.append(mapped)
            continue
        payload_parts.append(token)
        warnings.append(f"Unknown key token '{token}', sent as literal text.")

    return "".join(payload_parts), warnings


def _slice_process_log_lines(
    aggregated: str,
    *,
    offset: int | None,
    limit: int | None,
) -> tuple[str, int, bool, int, int]:
    """Slice aggregated logs by line window for pagination."""

    lines = aggregated.splitlines()
    total_lines = len(lines)
    using_default_tail = offset is None and limit is None

    if using_default_tail:
        start = max(0, total_lines - _PROCESS_DEFAULT_LOG_TAIL_LINES)
        end = total_lines
    else:
        start = max(0, int(offset or 0))
        if limit is None:
            end = total_lines
        else:
            safe_limit = max(0, min(int(limit), _PROCESS_MAX_LOG_LIMIT))
            end = min(total_lines, start + safe_limit)

    if start >= total_lines:
        return "", total_lines, using_default_tail, start, 0

    return "\n".join(lines[start:end]), total_lines, using_default_tail, start, max(0, end - start)


def _decode_process_hex(hex_values: list[str] | None) -> tuple[str, list[str]]:
    """Decode hex byte strings to control-byte text for stdin writes."""

    if not hex_values:
        return "", []

    chars: list[str] = []
    warnings: list[str] = []

    for raw in hex_values:
        token = (raw or "").strip().replace(" ", "")
        if token.lower().startswith("0x"):
            token = token[2:]
        if not token:
            continue
        if len(token) % 2 != 0:
            warnings.append(f"Invalid hex token '{raw}', expected even number of digits.")
            continue
        if not re.fullmatch(r"[0-9a-fA-F]+", token):
            warnings.append(f"Invalid hex token '{raw}', non-hex characters found.")
            continue
        for byte in bytes.fromhex(token):
            if byte > 0x7F:
                warnings.append(
                    f"Hex byte 0x{byte:02x} is outside ASCII range; skipped to avoid UTF-8 expansion."
                )
                continue
            chars.append(chr(byte))

    return "".join(chars), warnings


def _encode_process_paste(text: str, *, bracketed: bool) -> str:
    """Encode paste payload, optionally wrapped in bracketed-paste markers."""

    if not text:
        return ""
    if not bracketed:
        return text
    return f"\x1b[200~{text}\x1b[201~"


def _resolve_process_scope(scope: str | None) -> str | None:
    """Resolve process scope from explicit arg or current route context."""

    explicit = (scope or "").strip()
    if explicit:
        return explicit
    route_channel, route_chat_id = get_route()
    if route_channel and route_chat_id:
        return f"{route_channel}:{route_chat_id}"
    return None


def exec_command(
    command: str,
    working_dir: str | None = None,
    timeout: int = 60,
    yield_ms: int | None = None,
    background: bool = False,
    pty: bool = False,
    scope: str | None = None,
) -> str:
    """Execute a command safely and return combined output.

    Args:
        command: Command string. Simple commands run directly; shell syntax
            commands (e.g. export/&&/redirection) run via a shell.
        working_dir: Optional working directory; defaults to workspace root.
        timeout: Max execution time in seconds.
        yield_ms: Optional max wait time in milliseconds before returning a
            running background session.
        background: If True, return immediately with a background session id.
        pty: If True, request PTY mode (falls back to pipe mode when unsupported).
        scope: Optional process-session isolation scope. Defaults to current route.

    Returns:
        Foreground output (legacy behavior), or a background session message.

    Safety:
        - Enforces security policy flags (allowExec, execAllowlist, workspace path guard).
        - Blocks known destructive command patterns.
    """
    _debug(
        "tool.exec.input",
        {
            "command": command,
            "working_dir": working_dir,
            "timeout": timeout,
            "yield_ms": yield_ms,
            "background": background,
            "pty": pty,
            "scope": scope,
        },
    )
    cmd = command.strip()
    if not cmd:
        return _ret("tool.exec.output", "Error: command is empty")

    policy = _security_policy()
    if not policy.allow_exec:
        return _ret("tool.exec.output", "Error: exec is disabled by security policy")

    try:
        argv = shlex.split(cmd, posix=True)
    except ValueError as exc:
        return _ret("tool.exec.output", f"Error: invalid command syntax: {exc}")
    if not argv:
        return _ret("tool.exec.output", "Error: command is empty")

    security_error = _validate_exec_security(cmd, argv, policy)
    if security_error:
        return _ret("tool.exec.output", security_error)

    lower = cmd.lower()
    for pattern in _DENY_PATTERNS:
        if re.search(pattern, lower):
            return _ret("tool.exec.output", "Error: Command blocked by safety guard (dangerous pattern detected)")

    try:
        cwd = _resolve_path(working_dir, base_dir=_workspace(policy), policy=policy) if working_dir else _workspace(policy)
    except PermissionError as exc:
        return _ret("tool.exec.output", f"Error: {exc}")

    path_guard_error = _validate_exec_paths_for_command(cmd, argv, cwd, policy)
    if path_guard_error:
        return _ret("tool.exec.output", path_guard_error)

    command_argv = argv
    if _should_use_shell(argv):
        shell_argv = _build_shell_argv(cmd)
        if not shell_argv:
            return _ret("tool.exec.output", "Error: no compatible shell found for command execution")
        command_argv = shell_argv

    if not background and yield_ms is None and not pty:
        try:
            completed = subprocess.run(
                command_argv,
                shell=False,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return _ret("tool.exec.output", f"Error: Command timed out after {timeout} seconds")
        except Exception as exc:
            return _ret("tool.exec.output", f"Error executing command: {exc}")

        result = _format_exec_output(completed.stdout, completed.stderr, completed.returncode)
        _debug("tool.exec.output", {"chars": len(result), "preview": result[:240]})
        return result

    manager = get_process_session_manager()
    effective_scope = _resolve_process_scope(scope)
    try:
        session, warnings = manager.start_session(
            command=cmd,
            argv=command_argv,
            cwd=cwd,
            env=os.environ.copy(),
            use_pty=pty,
            scope_key=effective_scope,
        )
    except Exception as exc:
        return _ret("tool.exec.output", f"Error executing command: {exc}")

    yield_window = 0 if background else max(10, min(120_000, int(yield_ms or 10_000)))
    if yield_window == 0:
        manager.mark_backgrounded(session.session_id, scope_key=effective_scope)
        warning_text = "\n".join(warnings)
        result = (
            f"{warning_text}\n\n".lstrip()
            + f"Command still running (session {session.session_id}, pid {session.process.pid or 'n/a'}). "
            + "Use process(action='list'|'poll'|'log'|'write'|'send-keys'|'submit'|'paste'|'kill'|'remove') for follow-up."
        )
        return _ret("tool.exec.output", result)

    polled = manager.poll_session(session.session_id, timeout_ms=yield_window)
    if polled is None:
        return _ret("tool.exec.output", "Error: failed to read command output")

    if bool(polled.get("exited")):
        result = _format_exec_output(
            str(polled.get("stdout", "")),
            str(polled.get("stderr", "")),
            polled.get("exit_code") if isinstance(polled.get("exit_code"), int) else None,
        )
        manager.remove_session(session.session_id)
        _debug("tool.exec.output", {"chars": len(result), "preview": result[:240]})
        return result

    manager.mark_backgrounded(session.session_id, scope_key=effective_scope)
    warning_text = "\n".join(warnings)
    running = (
        f"{warning_text}\n\n".lstrip()
        + f"Command still running (session {session.session_id}, pid {session.process.pid or 'n/a'}). "
        + "Use process(action='list'|'poll'|'log'|'write'|'send-keys'|'submit'|'paste'|'kill'|'remove') for follow-up."
    )
    return _ret("tool.exec.output", running)


def process_session(
    action: str = "list",
    session_id: str | None = None,
    data: str = "",
    keys: list[str] | None = None,
    hex_values: list[str] | None = None,
    literal: str = "",
    offset: int | None = None,
    limit: int | None = None,
    timeout_ms: int = 0,
    bracketed: bool = True,
    eof: bool = False,
    scope: str | None = None,
) -> str:
    """Manage background exec sessions.

    Args:
        action: One of list/poll/log/write/send-keys/submit/paste/kill/remove.
        session_id: Required for all actions except list.
        data: Payload for write/paste.
        keys: Optional key tokens for send-keys, e.g. ["C-c", "Enter"].
        hex_values: Optional hex byte tokens for send-keys, e.g. ["03", "0d"].
        literal: Optional literal text payload for send-keys.
        offset: Optional line offset for `log` pagination.
        limit: Optional line limit for `log` pagination.
        timeout_ms: Optional wait window for poll.
        bracketed: Whether `paste` uses bracketed-paste wrappers.
        eof: Whether write should close stdin afterwards.
        scope: Optional process-session isolation scope. Defaults to current route.

    Returns:
        Human-readable action result, or an "Error: ..." message.
    """

    manager = get_process_session_manager()
    effective_scope = _resolve_process_scope(scope)
    normalized = (action or "").strip().lower()

    if normalized == "list":
        sessions = manager.list_sessions(scope_key=effective_scope)
        if not sessions:
            return _ret("tool.process.output", "No running or recent sessions.")
        lines = []
        now = dt.datetime.now().timestamp()
        for item in sessions:
            runtime = max(0, int(now - item.started_at))
            label = item.command.strip().replace("\n", " ")
            if len(label) > 100:
                label = label[:100] + "..."
            lines.append(
                f"{item.session_id} {item.status:9} {runtime:>4}s pid={item.pid or 'n/a'} :: {label}"
            )
        return _ret("tool.process.output", "\n".join(lines))

    if not (session_id or "").strip():
        return _ret("tool.process.output", "Error: session_id is required for this action")
    sid = session_id.strip()

    if normalized == "poll":
        payload = manager.poll_session(sid, timeout_ms=timeout_ms, scope_key=effective_scope)
        if payload is None:
            return _ret("tool.process.output", f"Error: No session found for {sid}")
        status = str(payload.get("status", "running"))
        retry_in_ms = payload.get("retry_in_ms")
        output = "\n".join(
            part
            for part in [
                str(payload.get("stdout", "")).strip(),
                str(payload.get("stderr", "")).strip(),
            ]
            if part
        )
        if not output:
            output = "(no new output)"
        if payload.get("exited"):
            exit_signal = payload.get("exit_signal")
            if status == "killed":
                trailer = "Process was killed."
            elif isinstance(exit_signal, int):
                trailer = f"Process exited with signal {exit_signal}."
            else:
                trailer = f"Process exited with code {payload.get('exit_code', 0)}."
        else:
            trailer = "Process still running."
            if isinstance(retry_in_ms, int):
                trailer += f" Suggested next poll in ~{retry_in_ms}ms."
        poll_meta = {
            "status": status,
            "retry_in_ms": retry_in_ms if isinstance(retry_in_ms, int) else None,
            "exit_code": payload.get("exit_code"),
            "exit_signal": payload.get("exit_signal"),
        }
        meta_prefix = f"[poll-meta]{json.dumps(poll_meta, ensure_ascii=False, separators=(',', ':'))}"
        return _ret("tool.process.output", f"{meta_prefix}\n\n{output}\n\n{trailer}")

    if normalized == "log":
        payload = manager.log_session(sid, scope_key=effective_scope)
        if payload is None:
            return _ret("tool.process.output", f"Error: No session found for {sid}")
        sliced, total_lines, using_default_tail, effective_offset, returned_lines = _slice_process_log_lines(
            str(payload.get("aggregated", "")),
            offset=offset,
            limit=limit,
        )
        text = sliced.strip() or "(no output yet)"
        if using_default_tail and total_lines > _PROCESS_DEFAULT_LOG_TAIL_LINES:
            text += (
                f"\n\n[showing last {_PROCESS_DEFAULT_LOG_TAIL_LINES} of {total_lines} lines; "
                "pass offset/limit to page]"
            )
        window_limit: int | None
        if using_default_tail:
            window_limit = _PROCESS_DEFAULT_LOG_TAIL_LINES
        elif limit is None:
            window_limit = None
        else:
            window_limit = max(0, min(int(limit), _PROCESS_MAX_LOG_LIMIT))
        log_meta = {
            "total_lines": total_lines,
            "offset": effective_offset,
            "returned_lines": returned_lines,
            "window_limit": window_limit,
            "truncated": bool(payload.get("truncated", False)),
        }
        meta_prefix = f"[log-meta]{json.dumps(log_meta, ensure_ascii=False, separators=(',', ':'))}"
        return _ret("tool.process.output", f"{meta_prefix}\n\n{text}")

    if normalized == "write":
        err = manager.write_session(sid, data, eof=eof, scope_key=effective_scope)
        if err:
            return _ret("tool.process.output", f"Error: {err}")
        suffix = " (stdin closed)" if eof else ""
        return _ret("tool.process.output", f"Wrote {len(data)} bytes to session {sid}{suffix}.")

    if normalized in {"send-keys", "send_keys"}:
        encoded_keys, key_warnings = _encode_process_keys(keys)
        encoded_hex, hex_warnings = _decode_process_hex(hex_values)
        payload = literal + encoded_keys + encoded_hex
        warnings = key_warnings + hex_warnings
        if not payload:
            return _ret("tool.process.output", "Error: send-keys requires keys, hex_values or literal")
        err = manager.write_session(sid, payload, eof=eof, scope_key=effective_scope)
        if err:
            return _ret("tool.process.output", f"Error: {err}")
        warning_text = f"\nWarnings:\n- " + "\n- ".join(warnings) if warnings else ""
        suffix = " (stdin closed)" if eof else ""
        return _ret(
            "tool.process.output",
            f"Sent {len(payload)} bytes to session {sid}{suffix}.{warning_text}",
        )

    if normalized == "submit":
        err = manager.write_session(sid, "\r", eof=False, scope_key=effective_scope)
        if err:
            return _ret("tool.process.output", f"Error: {err}")
        return _ret("tool.process.output", f"Submitted session {sid} (sent CR).")

    if normalized == "paste":
        payload = _encode_process_paste(data, bracketed=bracketed)
        err = manager.write_session(sid, payload, eof=False, scope_key=effective_scope)
        if err:
            return _ret("tool.process.output", f"Error: {err}")
        mode = "bracketed" if bracketed else "plain"
        return _ret("tool.process.output", f"Pasted {len(data)} chars to session {sid} ({mode}).")

    if normalized == "kill":
        err = manager.kill_session(sid, scope_key=effective_scope)
        if err:
            return _ret("tool.process.output", f"Error: {err}")
        return _ret("tool.process.output", f"Termination requested for session {sid}.")

    if normalized == "remove":
        removed = manager.remove_session(sid, scope_key=effective_scope)
        if not removed:
            return _ret("tool.process.output", f"Error: No session found for {sid}")
        return _ret("tool.process.output", f"Removed session {sid}.")

    return _ret("tool.process.output", f"Error: Unknown action '{action}'")


def _validate_http_url(url: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, "Only http/https URLs are supported."
        if not parsed.netloc:
            return False, "URL must include a domain."
        return True, ""
    except Exception as exc:
        return False, str(exc)


def browser(
    action: str,
    target_url: str | None = None,
    target_id: str | None = None,
    profile: str | None = None,
    target: str | None = None,
    node: str | None = None,
    timeout_ms: int | None = None,
    snapshot_format: str = "ai",
    request: str | None = None,
    paths: list[str] | None = None,
    ref: str | None = None,
    accept: bool | None = None,
    prompt_text: str | None = None,
    screenshot_path: str | None = None,
    screenshot_type: str | None = None,
) -> str:
    """Control the built-in browser runtime.

    Args:
        action: Browser action name. Supported now:
            ``status/start/stop/profiles/tabs/open/navigate/snapshot/screenshot/upload/dialog/act``.
        target_url: URL used by ``action="open"``.
        target_id: Optional tab target id for ``snapshot`` / ``act``.
        profile: Optional browser profile name (reserved for multi-profile iterations).
        target: Browser execution target. Supports ``host`` (default), ``node``, ``sandbox``.
        node: Optional node selector used with ``target="node"``.
        timeout_ms: Optional proxy timeout override for ``target=node|sandbox`` (milliseconds).
        snapshot_format: Snapshot format for ``action="snapshot"`` (``ai`` or ``aria``).
        request: Action payload used by ``action="act"``. Pass a JSON object
            string (for model tool-call compatibility).
        paths: Optional upload file paths for ``action="upload"``.
        ref: Optional selector/ref for ``action="upload"``.
        accept: Required bool for ``action="dialog"``.
        prompt_text: Optional prompt text for ``action="dialog"``.
        screenshot_path: Optional output file path for ``action="screenshot"``.
        screenshot_type: Optional image type for ``action="screenshot"`` (`png` or `jpeg`).

    Returns:
        JSON-formatted action result payload. On errors, returns
        ``{"ok": false, "error": ...}``.

    Notes:
        - Backend is selected by `OPENHERON_BROWSER_RUNTIME` (`playwright` or default memory).
        - Remote routing:
          - ``target=node`` forwards to ``OPENHERON_BROWSER_NODE_PROXY_URL``.
          - ``target=sandbox`` forwards to ``OPENHERON_BROWSER_SANDBOX_PROXY_URL``.
          - Optional proxy auth headers are read from:
            ``OPENHERON_BROWSER_NODE_PROXY_TOKEN`` / ``OPENHERON_BROWSER_SANDBOX_PROXY_TOKEN``
            / fallback ``OPENHERON_BROWSER_PROXY_TOKEN``.
        - ``node`` is only valid when ``target="node"``.
    """

    _debug(
        "tool.browser.input",
        {
            "action": action,
            "target_url": target_url,
            "target_id": target_id,
            "profile": profile,
            "target": target,
            "node": node,
            "timeout_ms": timeout_ms,
            "snapshot_format": snapshot_format,
            "request": request,
            "paths": paths,
            "ref": ref,
            "accept": accept,
            "prompt_text": prompt_text,
            "screenshot_path": screenshot_path,
            "screenshot_type": screenshot_type,
        },
    )

    normalized = (action or "").strip().lower()
    query: dict[str, Any] = {}
    browser_auth_token = os.getenv("OPENHERON_BROWSER_CONTROL_TOKEN", "").strip() or None
    browser_mutation_token = (
        os.getenv("OPENHERON_BROWSER_MUTATION_TOKEN", "").strip() or browser_auth_token
    )
    if (profile or "").strip():
        query["profile"] = profile.strip()
    if (target or "").strip():
        # Iteration 1 keeps the shape for future host/sandbox/node routing.
        query["target"] = target.strip()
    if (node or "").strip():
        query["node"] = node.strip()
    if timeout_ms is not None:
        try:
            parsed_timeout_ms = int(timeout_ms)
        except (TypeError, ValueError):
            return _ret(
                "tool.browser.output",
                _json({"ok": False, "error": "timeout_ms must be a positive integer", "status": 400}),
            )
        if parsed_timeout_ms <= 0:
            return _ret(
                "tool.browser.output",
                _json({"ok": False, "error": "timeout_ms must be a positive integer", "status": 400}),
            )
        query["timeoutMs"] = parsed_timeout_ms

    act_request: dict[str, Any] | None
    if isinstance(request, str):
        raw_request = request.strip()
        if raw_request:
            try:
                parsed_request = json.loads(raw_request)
            except json.JSONDecodeError:
                return _ret(
                    "tool.browser.output",
                    _json({"ok": False, "error": "request must be a valid JSON object string", "status": 400}),
                )
            if not isinstance(parsed_request, dict):
                return _ret(
                    "tool.browser.output",
                    _json({"ok": False, "error": "request must decode to a JSON object", "status": 400}),
                )
            act_request = parsed_request
        else:
            act_request = None
    else:
        # Backward-compatibility for direct Python calls in tests/integration.
        act_request = request if isinstance(request, dict) else None

    def _req(
        *,
        method: str,
        path: str,
        query_value: dict[str, Any] | None = None,
        body_value: dict[str, Any] | None = None,
    ) -> BrowserDispatchRequest:
        is_mutating = method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
        return BrowserDispatchRequest(
            method=method,
            path=path,
            query=query_value,
            body=body_value,
            auth_token=browser_auth_token,
            mutation_token=browser_mutation_token if is_mutating else None,
        )

    request_map: dict[str, BrowserDispatchRequest] = {
        "status": _req(method="GET", path="/", query_value=query),
        "start": _req(method="POST", path="/start", query_value=query),
        "stop": _req(method="POST", path="/stop", query_value=query),
        "profiles": _req(method="GET", path="/profiles", query_value=query),
        "tabs": _req(method="GET", path="/tabs", query_value=query),
        "open": _req(
            method="POST",
            path="/tabs/open",
            query_value=query,
            body_value={"url": target_url},
        ),
        "snapshot": _req(
            method="GET",
            path="/snapshot",
            query_value={
                **query,
                "targetId": (target_id or "").strip() or None,
                "format": (snapshot_format or "ai").strip().lower() or "ai",
            },
        ),
        "navigate": _req(
            method="POST",
            path="/navigate",
            query_value=query,
            body_value={
                "targetId": (target_id or "").strip() or None,
                "url": target_url,
            },
        ),
        "screenshot": _req(
            method="POST",
            path="/screenshot",
            query_value=query,
            body_value={
                "targetId": (target_id or "").strip() or None,
                "type": (screenshot_type or "png").strip().lower() or "png",
                "path": (screenshot_path or "").strip() or None,
            },
        ),
        "upload": _req(
            method="POST",
            path="/hooks/file-chooser",
            query_value=query,
            body_value={
                "targetId": (target_id or "").strip() or None,
                "paths": paths or [],
                "ref": (ref or "").strip() or None,
            },
        ),
        "dialog": _req(
            method="POST",
            path="/hooks/dialog",
            query_value=query,
            body_value={
                "targetId": (target_id or "").strip() or None,
                "accept": accept,
                "promptText": (prompt_text or "").strip() or None,
            },
        ),
        "act": _req(
            method="POST",
            path="/act",
            query_value=query,
            body_value={
                "targetId": (target_id or "").strip() or None,
                "request": act_request,
            },
        ),
    }
    dispatch_req = request_map.get(normalized)
    if dispatch_req is None:
        return _ret(
            "tool.browser.output",
            _json(
                {
                    "ok": False,
                    "error": (
                        "unknown action; supported actions are "
                        "status,start,stop,profiles,tabs,open,navigate,snapshot,screenshot,upload,dialog,act"
                    ),
                }
            ),
        )

    def _parse_proxy_success_payload(raw_text: str) -> dict[str, Any]:
        text = (raw_text or "").strip()
        if not text:
            return {"ok": True}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid proxy response (non-JSON payload)", "status": 502}
        if isinstance(parsed, dict):
            # Node/sandbox proxy commonly wraps actual response as {"result": {...}}.
            wrapped = parsed.get("result")
            if isinstance(wrapped, dict):
                merged = dict(wrapped)
                if "files" in parsed and "files" not in merged:
                    merged["files"] = parsed["files"]
                return merged
            return parsed
        return {"ok": False, "error": "invalid proxy response payload type", "status": 502}

    def _parse_proxy_error_payload(code: int, detail_text: str, fallback: str) -> dict[str, Any]:
        text = (detail_text or "").strip()
        if not text:
            return {"ok": False, "error": fallback, "status": code}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "error": text, "status": code}
        if not isinstance(parsed, dict):
            return {"ok": False, "error": text, "status": code}
        error_text = str(parsed.get("error") or parsed.get("message") or text).strip() or fallback
        status_value = parsed.get("status")
        if isinstance(status_value, int):
            return {"ok": False, "error": error_text, "status": status_value}
        return {"ok": False, "error": error_text, "status": code}

    def _proxy_unavailable_payload(reason: Any) -> dict[str, Any]:
        # Keep error shape stable while exposing clearer connectivity classes.
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return {"ok": False, "error": "browser proxy timeout", "status": 504}
        if isinstance(reason, ConnectionRefusedError):
            return {"ok": False, "error": "browser proxy connection refused", "status": 503}
        reason_text = str(reason or "").strip().lower()
        if "timed out" in reason_text:
            return {"ok": False, "error": "browser proxy timeout", "status": 504}
        if "connection refused" in reason_text:
            return {"ok": False, "error": "browser proxy connection refused", "status": 503}
        if "name or service not known" in reason_text or "nodename nor servname provided" in reason_text:
            return {"ok": False, "error": "browser proxy dns resolution failed", "status": 503}
        return {"ok": False, "error": f"browser proxy unavailable: {reason}", "status": 503}

    normalized_target = (target or "").strip().lower()
    if (node or "").strip() and normalized_target != "node":
        return _ret(
            "tool.browser.output",
            _json({"ok": False, "error": 'node is only supported with target="node"', "status": 400}),
        )
    if normalized_target in {"node", "sandbox"}:
        proxy_url_env = (
            "OPENHERON_BROWSER_NODE_PROXY_URL"
            if normalized_target == "node"
            else "OPENHERON_BROWSER_SANDBOX_PROXY_URL"
        )
        proxy_token_env = (
            "OPENHERON_BROWSER_NODE_PROXY_TOKEN"
            if normalized_target == "node"
            else "OPENHERON_BROWSER_SANDBOX_PROXY_TOKEN"
        )
        proxy_base = os.getenv(proxy_url_env, "").strip()
        if not proxy_base:
            return _ret(
                "tool.browser.output",
                _json(
                    {
                        "ok": False,
                        "error": f'target "{normalized_target}" is not implemented yet',
                        "status": 501,
                    }
                ),
            )

        proxy_query = {
            key: value
            for key, value in (dispatch_req.query or {}).items()
            if key != "target" and value is not None
        }
        query_string = urlencode(proxy_query)
        full_url = f"{proxy_base.rstrip('/')}{dispatch_req.path}"
        if query_string:
            full_url = f"{full_url}?{query_string}"
        timeout_seconds = 20.0
        timeout_override = proxy_query.get("timeoutMs")
        if isinstance(timeout_override, int):
            timeout_seconds = max(0.1, min(timeout_override / 1000.0, 300.0))

        body_bytes = (
            json.dumps(dispatch_req.body, ensure_ascii=False).encode("utf-8")
            if dispatch_req.body is not None
            else None
        )
        headers = {"Accept": "application/json"}
        proxy_token = os.getenv(proxy_token_env, "").strip() or os.getenv(
            "OPENHERON_BROWSER_PROXY_TOKEN", ""
        ).strip()
        if proxy_token:
            headers["X-OpenHeron-Browser-Proxy-Token"] = proxy_token
        if body_bytes is not None:
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(
                Request(full_url, data=body_bytes, headers=headers, method=dispatch_req.method),
                timeout=timeout_seconds,
            ) as r:
                raw = r.read().decode("utf-8", errors="replace")
                payload = _parse_proxy_success_payload(raw)
                return _ret("tool.browser.output", _json(payload))
        except HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            payload = _parse_proxy_error_payload(e.code, detail, str(e))
            return _ret("tool.browser.output", _json(payload))
        except URLError as e:
            return _ret("tool.browser.output", _json(_proxy_unavailable_payload(e.reason)))

    res = get_browser_control_service().dispatch(dispatch_req)
    body = res.body if isinstance(res.body, dict) else {"ok": False, "error": "invalid browser response"}
    if res.status >= 400 and isinstance(body, dict) and "status" not in body:
        body["status"] = res.status
    return _ret("tool.browser.output", _json(body))


def web_search(query: str, count: int = 5) -> str:
    """Search the web via Brave Search and return summarized top results.

    Args:
        query: Search query text.
        count: Requested result count (bounded by runtime configuration).

    Returns:
        Plain-text list of search hits, "No results ...", or an "Error: ..." message.

    Notes:
        - Current provider support is Brave only.
        - Requires network enabled and BRAVE_API_KEY configured.
    """
    _debug("tool.web_search.input", {"query": query, "count": count})
    if not _security_policy().allow_network:
        return _ret("tool.web_search.output", "Error: network access is disabled by security policy")
    if not env_enabled("OPENHERON_WEB_ENABLED", default=True):
        return _ret("tool.web_search.output", "Error: web tools are disabled in configuration")
    if not env_enabled("OPENHERON_WEB_SEARCH_ENABLED", default=True):
        return _ret("tool.web_search.output", "Error: web_search is disabled in configuration")

    provider = os.getenv("OPENHERON_WEB_SEARCH_PROVIDER", "brave").strip().lower() or "brave"
    if provider != "brave":
        return _ret(
            "tool.web_search.output",
            f"Error: web_search provider '{provider}' is not supported yet (supported: brave)",
        )

    max_results_raw = os.getenv("OPENHERON_WEB_SEARCH_MAX_RESULTS", "10").strip()
    try:
        max_results = int(max_results_raw)
    except ValueError:
        max_results = 10
    max_results = min(max(max_results, 1), 10)

    api_key = os.getenv("BRAVE_API_KEY", "")
    if not api_key:
        return _ret("tool.web_search.output", "Error: BRAVE_API_KEY not configured")
    n = min(max(count, 1), max_results)
    url = f"https://api.search.brave.com/res/v1/web/search?q={query}&count={n}"
    req = Request(
        url,
        headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        method="GET",
    )
    try:
        with urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        results = payload.get("web", {}).get("results", [])
        if not results:
            return _ret("tool.web_search.output", f"No results for: {query}")
        lines = [f"Results for: {query}", ""]
        for idx, item in enumerate(results[:n], start=1):
            lines.append(f"{idx}. {item.get('title', '')}")
            lines.append(f"   {item.get('url', '')}")
            description = item.get("description", "")
            if description:
                lines.append(f"   {description}")
        result = "\n".join(lines)
        _debug("tool.web_search.output", {"chars": len(result), "results": len(results[:n])})
        return result
    except HTTPError as exc:
        return _ret("tool.web_search.output", f"Error: HTTP {exc.code} from Brave Search")
    except URLError as exc:
        return _ret("tool.web_search.output", f"Error: Network error: {exc.reason}")
    except Exception as exc:
        return _ret("tool.web_search.output", f"Error: {exc}")


def web_fetch(url: str, max_chars: int = 50000) -> str:
    """Fetch a URL and return structured extraction as JSON text.

    Args:
        url: Target URL (http/https only).
        max_chars: Max extracted text length before truncation.

    Returns:
        JSON string with fields like url/finalUrl/status/extractor/truncated/text,
        or JSON-formatted error payload.
    """
    _debug("tool.web_fetch.input", {"url": url, "max_chars": max_chars})
    if not _security_policy().allow_network:
        return _ret("tool.web_fetch.output", _json({"error": "network access is disabled by security policy", "url": url}))
    ok, err = _validate_http_url(url)
    if not ok:
        return _ret("tool.web_fetch.output", _json({"error": err, "url": url}))

    req = Request(url, headers={"User-Agent": "openheron/0.1"}, method="GET")
    try:
        with urlopen(req, timeout=30) as response:
            status = getattr(response, "status", 200)
            final_url = getattr(response, "url", url)
            ctype = response.headers.get("Content-Type", "")
            raw = response.read()
        text = raw.decode("utf-8", errors="replace")
        if "application/json" in ctype:
            extracted = text
            extractor = "json"
        elif "text/html" in ctype or "<html" in text[:1024].lower():
            no_script = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
            no_style = re.sub(r"<style[\s\S]*?</style>", "", no_script, flags=re.I)
            extracted = re.sub(r"<[^>]+>", "", no_style)
            extracted = re.sub(r"[ \t]+", " ", extracted)
            extracted = re.sub(r"\n{3,}", "\n\n", extracted).strip()
            extractor = "html"
        else:
            extracted = text
            extractor = "raw"

        truncated = len(extracted) > max_chars
        if truncated:
            extracted = extracted[:max_chars]
        result = _json(
            {
                "url": url,
                "finalUrl": final_url,
                "status": status,
                "extractor": extractor,
                "truncated": truncated,
                "length": len(extracted),
                "text": extracted,
            }
        )
        _debug("tool.web_fetch.output", {"url": url, "status": status, "extractor": extractor, "chars": len(result)})
        return result
    except HTTPError as exc:
        return _ret("tool.web_fetch.output", _json({"error": f"HTTP {exc.code}", "url": url}))
    except URLError as exc:
        return _ret("tool.web_fetch.output", _json({"error": f"Network error: {exc.reason}", "url": url}))
    except Exception as exc:
        return _ret("tool.web_fetch.output", _json({"error": str(exc), "url": url}))


def configure_outbound_publisher(
    publisher: Callable[[OutboundMessage], Awaitable[None]] | None,
) -> None:
    """Configure optional outbound publishing callback used by gateway."""
    global _OUTBOUND_PUBLISHER
    _OUTBOUND_PUBLISHER = publisher


def configure_subagent_dispatcher(
    dispatcher: Callable[[SubagentSpawnRequest], None] | None,
) -> None:
    """Configure optional background sub-agent dispatcher used by gateway."""

    global _SUBAGENT_DISPATCHER
    _SUBAGENT_DISPATCHER = dispatcher


def _resolve_route(channel: str | None, chat_id: str | None) -> tuple[str, str]:
    route_channel, route_chat_id = get_route()
    final_channel = channel or route_channel or "local"
    final_chat_id = chat_id or route_chat_id or "default"
    return final_channel, final_chat_id


def _publish_outbound_if_configured(msg: OutboundMessage) -> bool:
    if _OUTBOUND_PUBLISHER is None:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Tool calls often happen in plain sync contexts (tests or direct calls).
        # In that case we intentionally fall back to local outbox logging.
        return False
    # Fire-and-forget is sufficient here: channel delivery is handled by gateway.
    loop.create_task(_OUTBOUND_PUBLISHER(msg))
    return True


def _append_outbox_record(record: dict[str, Any]) -> Path:
    """Append one outbound record to local outbox log and return the log path.

    The function always injects a timestamp so callers only provide channel-
    specific payload fields.
    """
    outbox = _workspace() / "messages" / "outbox.log"
    outbox.parent.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    line = json.dumps(
        {
            "timestamp": ts,
            **record,
        },
        ensure_ascii=False,
    )
    with outbox.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return outbox


def _append_subagent_record(record: dict[str, Any]) -> Path:
    """Append one sub-agent spawn record to local JSONL log.

    The record is written only when ``spawn_subagent`` successfully dispatches
    the task. The log is used by CLI introspection (`openheron spawn`).
    """
    log_path = _workspace() / ".openheron" / "subagents.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    line = json.dumps({"timestamp": ts, **record}, ensure_ascii=False)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return log_path


def message(content: str, channel: str | None = None, chat_id: str | None = None) -> str:
    """Send an outbound text message to a channel target.

    Args:
        content: Message content to send.
        channel: Optional channel override (e.g. "local", "feishu").
        chat_id: Optional target conversation/user id.

    Returns:
        Queue success message when gateway publisher is active; otherwise a local
        outbox write confirmation.

    Routing:
        - Uses explicit channel/chat_id first.
        - Falls back to current route context.
        - Final fallback is local/default.
    """
    target_channel, target_chat_id = _resolve_route(channel, chat_id)
    _debug("tool.message.input", {"channel": target_channel, "chat_id": target_chat_id, "chars": len(content)})

    outbound = OutboundMessage(channel=target_channel, chat_id=target_chat_id, content=content)
    if _publish_outbound_if_configured(outbound):
        result = f"Message queued to {target_channel}:{target_chat_id}"
        _debug("tool.message.output", result)
        return result

    outbox = _append_outbox_record(
        {
            "channel": target_channel,
            "chat_id": target_chat_id,
            "content": content,
        }
    )
    result = f"Message recorded to {outbox}"
    _debug("tool.message.output", result)
    return result


def spawn_subagent(
    prompt: str,
    notify_on_complete: bool = True,
    channel: str | None = None,
    chat_id: str | None = None,
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Spawn a background sub-agent task and return a pending ticket.

    This function is intended to be wrapped by ADK ``LongRunningFunctionTool``.
    It only creates and dispatches a task request. The real work runs in the
    runtime layer (gateway worker), outside this tool call.

    Args:
        prompt: Sub-task instruction that the background sub-agent should run.
        notify_on_complete: Whether runtime should push completion notification.
        channel: Optional channel override for completion notification.
        chat_id: Optional chat target override for completion notification.
        tool_context: ADK-injected tool context, used to capture invocation IDs.

    Returns:
        A structured payload with ``status`` and ``task_id``.
    """

    _debug(
        "tool.spawn_subagent.input",
        {
            "prompt_chars": len(prompt or ""),
            "notify_on_complete": bool(notify_on_complete),
            "channel": channel,
            "chat_id": chat_id,
        },
    )

    if not (prompt or "").strip():
        result = {"status": "error", "error": "prompt is required"}
        _debug("tool.spawn_subagent.output", result)
        return result

    if _SUBAGENT_DISPATCHER is None:
        result = {"status": "error", "error": "subagent dispatcher is not configured"}
        _debug("tool.spawn_subagent.output", result)
        return result

    if tool_context is None:
        result = {"status": "error", "error": "tool_context is required"}
        _debug("tool.spawn_subagent.output", result)
        return result

    user_id = getattr(tool_context, "user_id", None)
    session = getattr(tool_context, "session", None)
    session_id = getattr(session, "id", None) if session is not None else None
    invocation_id = getattr(tool_context, "invocation_id", None)
    function_call_id = getattr(tool_context, "function_call_id", None)
    if not (user_id and session_id and invocation_id and function_call_id):
        result = {
            "status": "error",
            "error": (
                "missing invocation metadata in tool context "
                "(need user_id/session_id/invocation_id/function_call_id)"
            ),
        }
        _debug("tool.spawn_subagent.output", result)
        return result

    target_channel, target_chat_id = _resolve_route(channel, chat_id)
    task_id = f"subagent-{uuid.uuid4().hex[:12]}"
    request = SubagentSpawnRequest(
        task_id=task_id,
        prompt=prompt,
        user_id=user_id,
        session_id=session_id,
        invocation_id=invocation_id,
        function_call_id=function_call_id,
        channel=target_channel,
        chat_id=target_chat_id,
        notify_on_complete=bool(notify_on_complete),
    )
    try:
        _SUBAGENT_DISPATCHER(request)
    except Exception as exc:
        result = {"status": "error", "error": f"failed to dispatch subagent task: {exc}"}
        _debug("tool.spawn_subagent.output", result)
        return result

    # Persist an accepted task ticket for CLI introspection and auditability.
    try:
        _append_subagent_record(
            {
                "status": "pending",
                "task_id": task_id,
                "prompt_preview": prompt.strip()[:200],
                "prompt_chars": len(prompt),
                "notify_on_complete": bool(notify_on_complete),
                "channel": target_channel,
                "chat_id": target_chat_id,
                "user_id": user_id,
                "session_id": session_id,
                "invocation_id": invocation_id,
                "function_call_id": function_call_id,
            }
        )
    except Exception as exc:
        _debug("tool.spawn_subagent.record_error", {"task_id": task_id, "error": str(exc)})

    result = {
        "status": "pending",
        "task_id": task_id,
        "message": "Sub-agent task accepted and running in background.",
    }
    _debug("tool.spawn_subagent.output", result)
    return result


def message_image(path: str, caption: str = "", channel: str | None = None, chat_id: str | None = None) -> str:
    """Send an outbound image message (optionally with caption).

    Args:
        path: Path to local image file.
        caption: Optional caption text.
        channel: Optional channel override.
        chat_id: Optional target conversation/user id.

    Returns:
        Queue success message when gateway publisher is active; otherwise a local
        outbox write confirmation, or an "Error: ..." message.

    Notes:
        - Allowed suffixes: .png, .jpg, .jpeg, .webp, .gif, .bmp
    """
    target_channel, target_chat_id = _resolve_route(channel, chat_id)
    _debug(
        "tool.message_image.input",
        {"path": path, "caption_chars": len(caption), "channel": target_channel, "chat_id": target_chat_id},
    )
    try:
        image_path = _resolve_path(path)
    except PermissionError as exc:
        return _ret("tool.message_image.output", f"Error: {exc}")
    except Exception as exc:
        return _ret("tool.message_image.output", f"Error resolving image path: {exc}")

    if not image_path.exists():
        return _ret("tool.message_image.output", f"Error: File not found: {path}")
    if not image_path.is_file():
        return _ret("tool.message_image.output", f"Error: Not a file: {path}")
    if image_path.suffix.lower() not in _IMAGE_SUFFIXES:
        allowed = ", ".join(sorted(_IMAGE_SUFFIXES))
        return _ret(
            "tool.message_image.output",
            f"Error: Unsupported image extension '{image_path.suffix}'. Allowed: {allowed}",
        )

    outbound = OutboundMessage(
        channel=target_channel,
        chat_id=target_chat_id,
        content=caption,
        metadata={
            "content_type": "image",
            "image_path": str(image_path),
        },
    )
    if _publish_outbound_if_configured(outbound):
        result = f"Image queued to {target_channel}:{target_chat_id}"
        _debug("tool.message_image.output", result)
        return result

    outbox = _append_outbox_record(
        {
            "channel": target_channel,
            "chat_id": target_chat_id,
            "content": caption,
            "metadata": outbound.metadata,
        },
    )
    result = f"Image message recorded to {outbox}"
    _debug("tool.message_image.output", result)
    return result


def _cron_store_path() -> Path:
    return cron_store_path(_workspace())


def _cron_service() -> CronService:
    return CronService(_cron_store_path())


def _format_job_schedule(job: Any) -> str:
    return format_schedule(getattr(job, "schedule", None))


_CRON_MESSAGE_PREFIX = "message from cron task: "


def _prefixed_cron_message(message: str) -> str:
    """Ensure cron payload text carries a stable runtime-origin prefix."""
    text = message.strip()
    if text.startswith(_CRON_MESSAGE_PREFIX):
        return text
    return f"{_CRON_MESSAGE_PREFIX}{text}"


def cron(
    action: str,
    message: str = "",
    every_seconds: int | None = None,
    cron_expr: str | None = None,
    at: str | None = None,
    job_id: str | None = None,
    tz: str | None = None,
    deliver: bool | None = None,
    channel: str | None = None,
    chat_id: str | None = None,
) -> str:
    """Manage persisted cron jobs (scheduler + delivery metadata).

    Args:
        action: One of "add", "list", "remove".
        message: Prompt executed at trigger time (required for add). This is sent
            to the LLM as a new user message, so write it as an explicit
            instruction, not just a loose label; message must be an executable
            action instruction. The tool automatically prefixes it with
            "message from cron task: " before persistence/execution.
        every_seconds: Fixed interval schedule in seconds (add mode).
        cron_expr: Cron schedule expression, e.g. "0 9 * * 1-5" (add mode).
        at: One-time absolute ISO datetime string, e.g. "2026-02-18T17:30:00" (add mode).
        job_id: Job id for remove mode.
        tz: IANA timezone for cron_expr, e.g. "Asia/Shanghai".
        deliver: Whether cron execution result should be delivered outward.
            If omitted, defaults to True in this tool.
        channel: Optional delivery channel override.
        chat_id: Optional delivery target id override.

    Returns:
        Human-readable status string, or an "Error: ..." message.

    Important:
        - Provide exactly one schedule source for add: every_seconds OR cron_expr OR at.
        - `at` must be an absolute timestamp, not a relative phrase.
        - One-time `at` jobs are auto-deleted after execution.
        - `message` should clearly specify the expected action and output format.
          Good reminder example:
            "你是提醒助手。请只输出：时间到了。不要添加其他内容。"
          Good task example:
            "请检查项目状态并输出三条摘要，每条不超过20字。"
        - When `deliver=True`, gateway will automatically deliver the final LLM
          response to channel/chat_id. Usually no extra `message(...)` tool call
          is needed unless multi-message behavior is required.
    """
    _debug(
        "tool.cron.input",
        {
            "action": action,
            "message_chars": len(message),
            "every_seconds": every_seconds,
            "cron_expr": cron_expr,
            "at": at,
            "job_id": job_id,
            "tz": tz,
            "deliver": deliver,
            "channel": channel,
            "chat_id": chat_id,
        },
    )
    service = _cron_service()

    if action == "list":
        jobs = service.list_jobs(include_disabled=True)
        if not jobs:
            return _ret("tool.cron.output", "No scheduled jobs.")
        lines = ["Scheduled jobs:"]
        for job in jobs:
            lines.append(f"- {job.name} (id: {job.id}, {_format_job_schedule(job)})")
        result = "\n".join(lines)
        _debug("tool.cron.output", {"action": action, "jobs": len(jobs)})
        return result

    if action == "remove":
        if not job_id:
            return _ret("tool.cron.output", "Error: job_id is required for remove")
        if not service.remove_job(job_id):
            return _ret("tool.cron.output", f"Job {job_id} not found")
        result = f"Removed job {job_id}"
        _debug("tool.cron.output", result)
        return result

    if action == "add":
        if not message:
            return _ret("tool.cron.output", "Error: message is required for add")
        parsed, parse_error = parse_schedule_input(
            every_seconds=every_seconds,
            cron_expr=cron_expr,
            at=at,
            tz=tz,
        )
        if parse_error:
            return _ret("tool.cron.output", f"Error: {parse_error}")
        if parsed is None:  # pragma: no cover - defensive fallback
            return _ret("tool.cron.output", "Error: failed to parse schedule")
        schedule = parsed.schedule
        delete_after_run = parsed.delete_after_run
        prefixed_message = _prefixed_cron_message(message)

        target_channel, target_chat_id = _resolve_route(channel, chat_id)
        deliver_enabled = True if deliver is None else bool(deliver)
        job = service.add_job(
            name=message[:30],
            schedule=schedule,
            message=prefixed_message,
            deliver=deliver_enabled,
            channel=target_channel,
            to=target_chat_id,
            delete_after_run=delete_after_run,
        )
        result = f"Created job '{job.name}' (id: {job.id})"
        _debug("tool.cron.output", result)
        return result

    return _ret("tool.cron.output", f"Unknown action: {action}")


# Match legacy tool naming where skills refer to `exec`.
exec_command.__name__ = "exec"
process_session.__name__ = "process"


def _debug(tag: str, payload: object, *, depth: int = 1) -> None:
    if not debug_logging_enabled():
        return
    emit_debug(tag, payload, depth=depth + 1)


def _ret(tag: str, value: str) -> str:
    # `_ret` is a thin helper; use depth=2 so the callsite points to the tool function line.
    _debug(tag, value, depth=2)
    return value
