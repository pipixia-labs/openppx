"""Core tools for sentientagent_v2 (except spawn)."""

from __future__ import annotations

import datetime as dt
import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .bus.events import OutboundMessage
from .env_utils import env_enabled
from .runtime.tool_context import get_route
from .security import PathGuard, SecurityPolicy, load_security_policy


_OUTBOUND_PUBLISHER: Callable[[OutboundMessage], Awaitable[None]] | None = None


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
    """Read the contents of a UTF-8 text file."""
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
    """Write UTF-8 text content into a file."""
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
    """Replace one exact text occurrence in a file."""
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
    """List entries in a directory."""
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


def _command_name(argv0: str) -> str:
    token = argv0.strip()
    if not token:
        return ""
    return Path(token).name if ("/" in token or "\\" in token) else token


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


def exec_command(command: str, working_dir: str | None = None, timeout: int = 60) -> str:
    """Execute a shell command and return stdout/stderr."""
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

    command_name = _command_name(argv[0])
    if not policy.is_exec_allowed(command_name):
        return _ret("tool.exec.output", f"Error: Command '{command_name}' is not in exec allowlist")

    lower = cmd.lower()
    for pattern in _DENY_PATTERNS:
        if re.search(pattern, lower):
            return _ret("tool.exec.output", "Error: Command blocked by safety guard (dangerous pattern detected)")

    try:
        cwd = _resolve_path(working_dir, base_dir=_workspace(policy), policy=policy) if working_dir else _workspace(policy)
    except PermissionError as exc:
        return _ret("tool.exec.output", f"Error: {exc}")

    path_guard_error = _validate_exec_paths(argv, cwd, policy)
    if path_guard_error:
        return _ret("tool.exec.output", path_guard_error)

    try:
        completed = subprocess.run(
            argv,
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
    """Search the web via Brave Search API."""
    _debug("tool.web_search.input", {"query": query, "count": count})
    if not _security_policy().allow_network:
        return _ret("tool.web_search.output", "Error: network access is disabled by security policy")
    if not env_enabled("SENTIENTAGENT_V2_WEB_ENABLED", default=True):
        return _ret("tool.web_search.output", "Error: web tools are disabled in configuration")
    if not env_enabled("SENTIENTAGENT_V2_WEB_SEARCH_ENABLED", default=True):
        return _ret("tool.web_search.output", "Error: web_search is disabled in configuration")

    provider = os.getenv("SENTIENTAGENT_V2_WEB_SEARCH_PROVIDER", "brave").strip().lower() or "brave"
    if provider != "brave":
        return _ret(
            "tool.web_search.output",
            f"Error: web_search provider '{provider}' is not supported yet (supported: brave)",
        )

    max_results_raw = os.getenv("SENTIENTAGENT_V2_WEB_SEARCH_MAX_RESULTS", "10").strip()
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
    """Fetch URL and return extracted text."""
    _debug("tool.web_fetch.input", {"url": url, "max_chars": max_chars})
    if not _security_policy().allow_network:
        return _ret("tool.web_fetch.output", _json({"error": "network access is disabled by security policy", "url": url}))
    ok, err = _validate_http_url(url)
    if not ok:
        return _ret("tool.web_fetch.output", _json({"error": err, "url": url}))

    req = Request(url, headers={"User-Agent": "sentientagent_v2/0.1"}, method="GET")
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


def message(content: str, channel: str | None = None, chat_id: str | None = None) -> str:
    """Send an outbound message via bus publisher or local outbox fallback."""
    target_channel, target_chat_id = _resolve_route(channel, chat_id)
    _debug("tool.message.input", {"channel": target_channel, "chat_id": target_chat_id, "chars": len(content)})

    outbound = OutboundMessage(channel=target_channel, chat_id=target_chat_id, content=content)
    if _publish_outbound_if_configured(outbound):
        result = f"Message queued to {target_channel}:{target_chat_id}"
        _debug("tool.message.output", result)
        return result

    outbox = _workspace() / "messages" / "outbox.log"
    outbox.parent.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    line = json.dumps(
        {"timestamp": ts, "channel": target_channel, "chat_id": target_chat_id, "content": content},
        ensure_ascii=False,
    )
    with outbox.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    result = f"Message recorded to {outbox}"
    _debug("tool.message.output", result)
    return result


def message_image(path: str, caption: str = "", channel: str | None = None, chat_id: str | None = None) -> str:
    """Send an outbound image message via bus publisher or local outbox fallback."""
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

    outbox = _workspace() / "messages" / "outbox.log"
    outbox.parent.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    line = json.dumps(
        {
            "timestamp": ts,
            "channel": target_channel,
            "chat_id": target_chat_id,
            "content": caption,
            "metadata": outbound.metadata,
        },
        ensure_ascii=False,
    )
    with outbox.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    result = f"Image message recorded to {outbox}"
    _debug("tool.message_image.output", result)
    return result


def _cron_store_path() -> Path:
    return _workspace() / ".sentientagent_v2" / "cron_jobs.json"


def _load_cron_jobs() -> list[dict[str, Any]]:
    path = _cron_store_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_cron_jobs(jobs: list[dict[str, Any]]) -> None:
    path = _cron_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


def cron(
    action: str,
    message: str = "",
    every_seconds: int | None = None,
    cron_expr: str | None = None,
    at: str | None = None,
    job_id: str | None = None,
) -> str:
    """Manage simple persisted cron jobs: add/list/remove."""
    _debug(
        "tool.cron.input",
        {
            "action": action,
            "message_chars": len(message),
            "every_seconds": every_seconds,
            "cron_expr": cron_expr,
            "at": at,
            "job_id": job_id,
        },
    )
    jobs = _load_cron_jobs()

    if action == "list":
        if not jobs:
            return _ret("tool.cron.output", "No scheduled jobs.")
        lines = ["Scheduled jobs:"]
        for j in jobs:
            lines.append(f"- {j['name']} (id: {j['id']}, {j['schedule']})")
        result = "\n".join(lines)
        _debug("tool.cron.output", {"action": action, "jobs": len(jobs)})
        return result

    if action == "remove":
        if not job_id:
            return _ret("tool.cron.output", "Error: job_id is required for remove")
        before = len(jobs)
        jobs = [j for j in jobs if j["id"] != job_id]
        if len(jobs) == before:
            return _ret("tool.cron.output", f"Job {job_id} not found")
        _save_cron_jobs(jobs)
        result = f"Removed job {job_id}"
        _debug("tool.cron.output", result)
        return result

    if action == "add":
        if not message:
            return _ret("tool.cron.output", "Error: message is required for add")
        schedule = ""
        if every_seconds:
            schedule = f"every:{every_seconds}s"
        elif cron_expr:
            schedule = f"cron:{cron_expr}"
        elif at:
            schedule = f"at:{at}"
        else:
            return _ret("tool.cron.output", "Error: either every_seconds, cron_expr, or at is required")

        new_job = {
            "id": uuid.uuid4().hex[:8],
            "name": message[:30],
            "message": message,
            "schedule": schedule,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        jobs.append(new_job)
        _save_cron_jobs(jobs)
        result = f"Created job '{new_job['name']}' (id: {new_job['id']})"
        _debug("tool.cron.output", result)
        return result

    return _ret("tool.cron.output", f"Unknown action: {action}")


# Match legacy tool naming where skills refer to `exec`.
exec_command.__name__ = "exec"


def _debug_enabled() -> bool:
    return env_enabled("SENTIENTAGENT_V2_DEBUG", default=False)


def _debug(tag: str, payload: object) -> None:
    if not _debug_enabled():
        return
    try:
        body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        body = str(payload)
    print(f"[DEBUG] {tag}: {body}", file=sys.stderr)


def _ret(tag: str, value: str) -> str:
    _debug(tag, value)
    return value
