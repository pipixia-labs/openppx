"""Core tools for openheron (except spawn)."""

from __future__ import annotations

import datetime as dt
import asyncio
import json
import os
import re
import shutil
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .bus.events import OutboundMessage
from .env_utils import env_enabled
from .exec_policy import command_segments as _policy_command_segments
from .exec_policy import validate_exec_security as _policy_validate_exec_security
from .logging_utils import debug_logging_enabled, emit_debug
from .runtime.cron_helpers import cron_store_path, format_schedule
from .runtime.cron_schedule_parser import parse_schedule_input
from .runtime.cron_service import CronService
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


def read_file(path: str) -> str:
    """Read a UTF-8 text file.

    Args:
        path: Absolute or workspace-relative file path.

    Returns:
        File content on success, otherwise an "Error: ..." message.

    Notes:
        - Path resolution follows security policy (workspace restriction may apply).
        - Intended for text files.
    """
    _debug("tool.read_file.input", {"path": path})
    try:
        target = _resolve_path(path)
        if not target.exists():
            return _ret("tool.read_file.output", f"Error: File not found: {path}")
        if not target.is_file():
            return _ret("tool.read_file.output", f"Error: Not a file: {path}")
        result = target.read_text(encoding="utf-8")
        _debug("tool.read_file.output", {"path": str(target), "chars": len(result)})
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


def exec_command(command: str, working_dir: str | None = None, timeout: int = 60) -> str:
    """Execute a command safely and return combined output.

    Args:
        command: Command string. Simple commands run directly; shell syntax
            commands (e.g. export/&&/redirection) run via a shell.
        working_dir: Optional working directory; defaults to workspace root.
        timeout: Max execution time in seconds.

    Returns:
        stdout/stderr text, optionally with exit code, or an "Error: ..." message.

    Safety:
        - Enforces security policy flags (allowExec, execAllowlist, workspace path guard).
        - Blocks known destructive command patterns.
    """
    _debug("tool.exec.input", {"command": command, "working_dir": working_dir, "timeout": timeout})
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

    parts: list[str] = []
    if completed.stdout:
        parts.append(completed.stdout)
    if completed.stderr:
        parts.append(f"STDERR:\n{completed.stderr}")
    if completed.returncode != 0:
        parts.append(f"Exit code: {completed.returncode}")
    result = "\n".join(parts).strip() or "(no output)"
    max_len = 12000
    if len(result) > max_len:
        result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"
    _debug("tool.exec.output", {"chars": len(result), "preview": result[:240]})
    return result


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


def _debug(tag: str, payload: object, *, depth: int = 1) -> None:
    if not debug_logging_enabled():
        return
    emit_debug(tag, payload, depth=depth + 1)


def _ret(tag: str, value: str) -> str:
    # `_ret` is a thin helper; use depth=2 so the callsite points to the tool function line.
    _debug(tag, value, depth=2)
    return value
