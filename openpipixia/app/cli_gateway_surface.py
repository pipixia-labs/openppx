"""Gateway lifecycle command handlers extracted from cli.py."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


def stop_gateway_pid(
    *,
    pid: int,
    timeout_seconds: float,
    is_pid_running: Callable[[int], bool],
) -> tuple[bool, bool]:
    """Stop one pid. Returns (stopped, forced)."""
    if pid <= 0:
        return True, False
    if not is_pid_running(pid):
        return True, False
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        return False, False

    deadline = time.time() + max(1.0, timeout_seconds)
    while time.time() < deadline:
        if not is_pid_running(pid):
            return True, False
        time.sleep(0.15)

    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        return False, False
    return (not is_pid_running(pid)), True


def cmd_gateway_start_single(
    *,
    channels: str | None,
    sender_id: str,
    chat_id: str,
    stdout_line: Callable[[str], None],
    read_gateway_pid: Callable[[], int | None],
    is_pid_running: Callable[[int], bool],
    gateway_cleanup_runtime_files: Callable[[], None],
    parse_enabled_channels: Callable[[str | None], list[str]],
    get_config_path: Callable[[], Path],
    gateway_debug_log_path: Callable[[], Path],
    gateway_stdout_log_path: Callable[[], Path],
    gateway_stderr_log_path: Callable[[], Path],
    write_gateway_runtime_metadata: Callable[[int, str, list[str]], None],
    gateway_log_dir: Callable[[], Path],
) -> int:
    existing = read_gateway_pid()
    if existing and is_pid_running(existing):
        stdout_line(f"Gateway service already running (pid={existing}).")
        stdout_line("Use `openpipixia gateway status` or `openpipixia gateway restart`.")
        return 0

    if existing and not is_pid_running(existing):
        gateway_cleanup_runtime_files()

    channels_value = ",".join(parse_enabled_channels(channels))
    config_path = get_config_path()
    cmd = [
        sys.executable,
        "-m",
        "openpipixia.app.cli",
        "--config-path",
        str(config_path),
        "gateway",
        "run",
        "--channels",
        channels_value,
        "--sender-id",
        sender_id,
        "--chat-id",
        chat_id,
    ]
    env = dict(os.environ)
    env["OPENPIPIXIA_GATEWAY_BG"] = "1"
    env["OPENPIPIXIA_DEBUG_LOG_PATH"] = str(gateway_debug_log_path())

    stdout_path = gateway_stdout_log_path()
    stderr_path = gateway_stderr_log_path()
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("a", encoding="utf-8") as stdout_fh, stderr_path.open("a", encoding="utf-8") as stderr_fh:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=stdout_fh,
                stderr=stderr_fh,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        except Exception as exc:
            stdout_line(f"Gateway service start failed: {exc}")
            return 1

    (stdout_path.parent / "gateway.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
    write_gateway_runtime_metadata(proc.pid, channels_value, cmd)
    stdout_line(f"Gateway service started (pid={proc.pid}).")
    stdout_line(f"Logs: {gateway_log_dir()}")
    return 0


def cmd_gateway_start_multi(
    *,
    channels: str | None,
    sender_id: str,
    chat_id: str,
    stdout_line: Callable[[str], None],
    parse_enabled_channels: Callable[[str | None], list[str]],
    global_enabled_agent_names: Callable[[], list[str]],
    agent_config_path: Callable[[str], Path],
    multi_agent_channel_conflict_warnings: Callable[[dict[str, Path]], list[str]],
    multi_agent_workspace_warnings: Callable[[dict[str, Path]], list[str]],
    agent_gateway_log_paths: Callable[[str, Path], tuple[Path, Path, Path]],
    write_gateway_multi_runtime_metadata: Callable[[str, list[dict[str, Any]]], None],
) -> int:
    channels_override = ",".join(parse_enabled_channels(channels)) if channels is not None else ""
    enabled_agents = global_enabled_agent_names()
    if not enabled_agents:
        stdout_line("Gateway multi-agent start skipped: no enabled agents in global_config.json.")
        return 1

    config_paths: dict[str, Path] = {}
    for agent_name in enabled_agents:
        config_path = agent_config_path(agent_name)
        if not config_path.exists():
            stdout_line(
                f"[warn] agent '{agent_name}' missing config: {config_path}. "
                "Skipping this agent."
            )
            continue
        config_paths[agent_name] = config_path
    if not config_paths:
        stdout_line("Gateway multi-agent start failed: no valid agent config file found.")
        return 1

    for warning in multi_agent_channel_conflict_warnings(config_paths):
        stdout_line(f"[warn] {warning}")
    for warning in multi_agent_workspace_warnings(config_paths):
        stdout_line(f"[warn] {warning}")

    started_entries: list[dict[str, Any]] = []
    failed_agents: list[str] = []
    for agent_name, config_path in config_paths.items():
        cmd = [
            sys.executable,
            "-m",
            "openpipixia.app.cli",
            "--config-path",
            str(config_path),
            "gateway",
            "run",
            "--sender-id",
            sender_id,
            "--chat-id",
            chat_id,
        ]
        if channels_override:
            cmd.extend(["--channels", channels_override])
        stdout_path, stderr_path, debug_path = agent_gateway_log_paths(agent_name, config_path)
        env = dict(os.environ)
        env["OPENPIPIXIA_GATEWAY_BG"] = "1"
        env["OPENPIPIXIA_DEBUG_LOG_PATH"] = str(debug_path)

        with stdout_path.open("a", encoding="utf-8") as stdout_fh, stderr_path.open("a", encoding="utf-8") as stderr_fh:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=stdout_fh,
                    stderr=stderr_fh,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env=env,
                )
            except Exception as exc:
                stdout_line(f"[warn] agent '{agent_name}' start failed: {exc}")
                failed_agents.append(agent_name)
                continue

        started_entries.append(
            {
                "agent": agent_name,
                "pid": proc.pid,
                "configPath": str(config_path),
                "startedAt": dt.datetime.now().astimezone().isoformat(),
                "command": cmd,
                "logs": {
                    "stdout": str(stdout_path),
                    "stderr": str(stderr_path),
                    "debug": str(debug_path),
                },
            }
        )
        stdout_line(f"Gateway agent started: agent={agent_name}, pid={proc.pid}")

    if not started_entries:
        stdout_line("Gateway multi-agent start failed: no agent process started.")
        return 1

    write_gateway_multi_runtime_metadata(channels_override, started_entries)
    stdout_line(f"Gateway multi-agent service started: agents={len(started_entries)}")
    if failed_agents:
        stdout_line(f"[warn] Failed agents: {', '.join(sorted(failed_agents))}")
        return 1
    return 0


def cmd_gateway_stop_single(
    *,
    timeout_seconds: float,
    stdout_line: Callable[[str], None],
    read_gateway_pid: Callable[[], int | None],
    is_pid_running: Callable[[int], bool],
    gateway_cleanup_runtime_files: Callable[[], None],
) -> int:
    pid = read_gateway_pid()
    if not pid:
        stdout_line("Gateway service is not running (no pid file).")
        return 0
    if not is_pid_running(pid):
        gateway_cleanup_runtime_files()
        stdout_line("Gateway service is not running (stale pid file removed).")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as exc:
        stdout_line(f"Gateway service stop failed: {exc}")
        return 1

    deadline = time.time() + max(1.0, timeout_seconds)
    while time.time() < deadline:
        if not is_pid_running(pid):
            gateway_cleanup_runtime_files()
            stdout_line("Gateway service stopped.")
            return 0
        time.sleep(0.15)

    try:
        os.kill(pid, signal.SIGKILL)
    except Exception as exc:
        stdout_line(f"Gateway service stop timeout and kill failed: {exc}")
        return 1

    gateway_cleanup_runtime_files()
    stdout_line("Gateway service stopped (forced).")
    return 0


def cmd_gateway_stop_multi(
    *,
    timeout_seconds: float,
    stdout_line: Callable[[str], None],
    read_gateway_multi_runtime_metadata: Callable[[], dict[str, Any]],
    gateway_cleanup_multi_runtime_files: Callable[[], None],
    collect_running_multi_agent_entries: Callable[[dict[str, Any]], list[dict[str, Any]]],
    write_gateway_multi_runtime_metadata: Callable[[str, list[dict[str, Any]]], None],
    stop_gateway_pid_fn: Callable[[int, float], tuple[bool, bool]],
) -> int:
    meta = read_gateway_multi_runtime_metadata()
    entries = meta.get("agents")
    if not isinstance(entries, list) or not entries:
        gateway_cleanup_multi_runtime_files()
        stdout_line("Gateway multi-agent service is not running.")
        return 0

    failures: list[str] = []
    forced_count = 0
    stopped_count = 0
    for item in entries:
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "unknown")).strip() or "unknown"
        try:
            pid = int(item.get("pid", 0))
        except Exception:
            pid = 0
        if pid <= 0:
            continue
        stopped, forced = stop_gateway_pid_fn(pid, timeout_seconds)
        if not stopped:
            failures.append(f"{agent}(pid={pid})")
            continue
        if forced:
            forced_count += 1
        stopped_count += 1

    still_running = collect_running_multi_agent_entries(meta)
    if still_running:
        write_gateway_multi_runtime_metadata(str(meta.get("channelsOverride", "")).strip(), still_running)
    else:
        gateway_cleanup_multi_runtime_files()

    if failures or still_running:
        if failures:
            stdout_line(f"Gateway multi-agent stop failed for: {', '.join(failures)}")
        if still_running:
            names = [str(item.get("agent", "unknown")) for item in still_running]
            stdout_line(f"Gateway multi-agent still running: {', '.join(names)}")
        return 1

    suffix = " (forced)" if forced_count > 0 else ""
    stdout_line(f"Gateway multi-agent service stopped: agents={stopped_count}{suffix}.")
    return 0


def cmd_gateway_status_single(
    *,
    output_json: bool,
    stdout_line: Callable[[str], None],
    read_gateway_pid: Callable[[], int | None],
    is_pid_running: Callable[[int], bool],
    gateway_cleanup_runtime_files: Callable[[], None],
    read_gateway_runtime_metadata: Callable[[], dict[str, Any]],
    gateway_log_dir: Callable[[], Path],
    gateway_stdout_log_path: Callable[[], Path],
    gateway_stderr_log_path: Callable[[], Path],
    gateway_debug_log_path: Callable[[], Path],
) -> int:
    pid = read_gateway_pid()
    running = bool(pid and is_pid_running(pid))
    if pid and not running:
        gateway_cleanup_runtime_files()
        pid = None
    meta = read_gateway_runtime_metadata()
    payload: dict[str, Any] = {
        "running": running,
        "pid": pid,
        "logsDir": str(gateway_log_dir()),
        "stdoutLog": str(gateway_stdout_log_path()),
        "stderrLog": str(gateway_stderr_log_path()),
        "debugLog": str(gateway_debug_log_path()),
    }
    if meta:
        payload["meta"] = meta

    if output_json:
        stdout_line(json.dumps(payload, ensure_ascii=False))
        return 0

    if not running:
        stdout_line("Gateway service status: stopped")
        stdout_line(f"Logs directory: {payload['logsDir']}")
        return 0

    stdout_line("Gateway service status: " f"running pid={pid}, logs={payload['logsDir']}")
    if isinstance(meta, dict):
        channels = str(meta.get("channels", "")).strip()
        started_at = str(meta.get("startedAt", "")).strip()
        if channels:
            stdout_line(f"Channels: {channels}")
        if started_at:
            stdout_line(f"Started at: {started_at}")
    return 0


def cmd_gateway_status_multi(
    *,
    output_json: bool,
    stdout_line: Callable[[str], None],
    read_gateway_multi_runtime_metadata: Callable[[], dict[str, Any]],
    is_pid_running: Callable[[int], bool],
    gateway_log_dir: Callable[[], Path],
) -> int:
    meta = read_gateway_multi_runtime_metadata()
    entries = meta.get("agents")
    if not isinstance(entries, list):
        entries = []
    rows: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        try:
            pid = int(row.get("pid", 0))
        except Exception:
            pid = 0
        row["running"] = bool(pid and is_pid_running(pid))
        rows.append(row)
    running_rows = [row for row in rows if bool(row.get("running"))]

    payload: dict[str, Any] = {
        "mode": "multi-agent",
        "running": bool(running_rows),
        "runningCount": len(running_rows),
        "agentCount": len(rows),
        "logsDir": str(gateway_log_dir()),
        "meta": {
            "channelsOverride": str(meta.get("channelsOverride", "")).strip(),
            "startedAt": str(meta.get("startedAt", "")).strip(),
        },
        "agents": rows,
    }
    if output_json:
        stdout_line(json.dumps(payload, ensure_ascii=False))
        return 0

    if not rows:
        stdout_line("Gateway multi-agent service status: stopped")
        stdout_line(f"Logs directory: {payload['logsDir']}")
        return 0

    stdout_line(
        "Gateway multi-agent service status: "
        f"running={payload['runningCount']}/{payload['agentCount']}, logs={payload['logsDir']}"
    )
    for row in rows:
        agent = str(row.get("agent", "unknown"))
        pid = row.get("pid")
        state = "running" if row.get("running") else "stopped"
        stdout_line(f"- {agent}: {state}, pid={pid}")
    channels_override = str(meta.get("channelsOverride", "")).strip()
    if channels_override:
        stdout_line(f"Channels override: {channels_override}")
    return 0


def cmd_gateway_restart(*, stop_fn: Callable[[], int], start_fn: Callable[[], int]) -> int:
    stop_code = stop_fn()
    if stop_code != 0:
        return stop_code
    return start_fn()
