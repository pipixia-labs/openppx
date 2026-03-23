"""Command-chain parsing and exec security policy helpers."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Iterable

from .security import SecurityPolicy

_EXEC_SECURITY_MODES = {"deny", "allowlist", "full"}
_EXEC_ASK_MODES = {"off", "on-miss", "always"}
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def split_command_chain(command: str) -> list[str] | None:
    """Split shell command by && / || / ; while respecting quotes and escapes."""
    parts: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    found_chain = False
    invalid_chain = False

    def _push_part() -> None:
        nonlocal invalid_chain
        chunk = "".join(buf).strip()
        buf.clear()
        if not chunk:
            invalid_chain = True
            return
        parts.append(chunk)

    idx = 0
    while idx < len(command):
        ch = command[idx]
        nxt = command[idx + 1] if idx + 1 < len(command) else ""

        if escaped:
            buf.append(ch)
            escaped = False
            idx += 1
            continue

        if not in_single and ch == "\\":
            escaped = True
            buf.append(ch)
            idx += 1
            continue

        if in_single:
            if ch == "'":
                in_single = False
            buf.append(ch)
            idx += 1
            continue

        if in_double:
            if ch == '"':
                in_double = False
            buf.append(ch)
            idx += 1
            continue

        if ch == "'":
            in_single = True
            buf.append(ch)
            idx += 1
            continue

        if ch == '"':
            in_double = True
            buf.append(ch)
            idx += 1
            continue

        if ch == "&" and nxt == "&":
            _push_part()
            found_chain = True
            idx += 2
            continue

        if ch == "|" and nxt == "|":
            _push_part()
            found_chain = True
            idx += 2
            continue

        if ch == ";":
            _push_part()
            found_chain = True
            idx += 1
            continue

        buf.append(ch)
        idx += 1

    tail = "".join(buf).strip()
    if found_chain:
        if tail:
            parts.append(tail)
        else:
            invalid_chain = True
        if invalid_chain:
            return None
        return parts

    return None


def command_segments(command: str, argv: list[str]) -> list[list[str]]:
    """Return command segments split by chain operators (&&/||/;)."""
    chain_parts = split_command_chain(command)
    if not chain_parts:
        return [argv]

    segments: list[list[str]] = []
    for part in chain_parts:
        try:
            part_argv = shlex.split(part, posix=True)
        except ValueError:
            continue
        if part_argv:
            segments.append(part_argv)
    return segments or [argv]


def _command_name(argv0: str) -> str:
    token = argv0.strip()
    if not token:
        return ""
    return Path(token).name if ("/" in token or "\\" in token) else token


def _segment_command_name(argv: list[str]) -> str:
    idx = 0
    while idx < len(argv) and _ENV_ASSIGNMENT_RE.match(argv[idx]):
        idx += 1
    if idx >= len(argv):
        return ""
    return _command_name(argv[idx])


def _normalize_exec_name(name: str) -> str:
    text = name.strip().lower()
    if text.endswith(".exe"):
        text = text[:-4]
    return text


def _parse_exec_safe_bins() -> set[str]:
    raw = os.getenv("OPENPIPIXIA_EXEC_SAFE_BINS", "")
    safe_bins: set[str] = set()
    for token in raw.split(","):
        name = _normalize_exec_name(token)
        if name:
            safe_bins.add(name)
    return safe_bins


def _resolve_exec_security_mode(policy: SecurityPolicy) -> tuple[str | None, str]:
    raw = os.getenv("OPENPIPIXIA_EXEC_SECURITY", "").strip().lower()
    if not raw:
        return None, "allowlist" if policy.exec_allowlist else "full"
    if raw not in _EXEC_SECURITY_MODES:
        return f"Error: invalid OPENPIPIXIA_EXEC_SECURITY '{raw}' (expected deny|allowlist|full)", ""
    return None, raw


def _resolve_exec_ask_mode() -> tuple[str | None, str]:
    raw = os.getenv("OPENPIPIXIA_EXEC_ASK", "").strip().lower()
    if not raw:
        return None, "off"
    if raw not in _EXEC_ASK_MODES:
        return f"Error: invalid OPENPIPIXIA_EXEC_ASK '{raw}' (expected off|on-miss|always)", ""
    return None, raw


def validate_exec_security(
    *,
    command: str,
    argv: list[str],
    policy: SecurityPolicy,
    shell_builtins: Iterable[str],
) -> str | None:
    """Validate command against configured exec security mode."""
    builtins = set(shell_builtins)
    mode_error, mode = _resolve_exec_security_mode(policy)
    if mode_error:
        return mode_error
    ask_error, ask_mode = _resolve_exec_ask_mode()
    if ask_error:
        return ask_error
    if ask_mode == "always":
        return "Error: approval required to execute command (ask=always)"
    if mode == "deny":
        return "Error: exec denied by security policy (mode=deny)"
    if mode == "full":
        return None

    safe_bins = _parse_exec_safe_bins()
    for segment in command_segments(command, argv):
        command_name = _segment_command_name(segment)
        if not command_name or command_name in builtins:
            continue
        if policy.is_exec_allowed(command_name):
            continue
        if _normalize_exec_name(command_name) in safe_bins:
            continue
        if ask_mode == "on-miss":
            return (
                "Error: approval required to execute command "
                f"(ask=on-miss, command='{command_name}')"
            )
        return f"Error: Command '{command_name}' is not in exec allowlist"
    return None
