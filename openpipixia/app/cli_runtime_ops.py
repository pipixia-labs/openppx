"""Runtime ops command handlers extracted from cli.py."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..runtime.cron_helpers import cron_store_path, format_schedule, format_timestamp_ms
from ..runtime.cron_service import CronService
from ..runtime.cron_schedule_parser import parse_schedule_input
from ..runtime.heartbeat_status_store import read_heartbeat_status_snapshot
from ..runtime.token_usage_store import parse_time_filter_to_epoch_ms


def _format_response_at(raw: Any, *, display_utc: bool) -> str:
    """Format one response timestamp in UTC or local timezone for text output."""
    value = str(raw or "").strip()
    if not value:
        return "-"
    if display_utc:
        return value
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return value
    if dt.tzinfo is None:
        return value
    return dt.astimezone().isoformat()


def _format_recent_reason_counts(raw_counts: Any) -> str:
    """Render recent heartbeat trigger sources as sorted count/percentage text."""
    if not isinstance(raw_counts, dict):
        return "-"
    normalized: dict[str, int] = {}
    for key, value in raw_counts.items():
        name = str(key or "").strip().lower() or "other"
        try:
            count = int(value)
        except Exception:
            continue
        if count <= 0:
            continue
        normalized[name] = normalized.get(name, 0) + count
    if not normalized:
        return "-"
    total = sum(normalized.values())
    ordered = sorted(normalized.items(), key=lambda item: (-item[1], item[0]))
    parts = [f"{name}={count} ({round((count * 100.0) / total):.0f}%)" for name, count in ordered]
    return f"(last {total}): " + ", ".join(parts)


def cron_service_for_agent(
    *,
    agent_name: str,
    agent_config_path: Callable[[str], Path],
    load_config_fn: Callable[..., dict[str, Any]],
) -> tuple[CronService | None, str | None]:
    """Build one cron service directly from one agent config/workspace."""
    config_path = agent_config_path(agent_name)
    if not config_path.exists():
        return None, f"agent '{agent_name}' config not found: {config_path}"
    try:
        cfg = load_config_fn(config_path=config_path)
    except Exception as exc:
        return None, f"agent '{agent_name}' config load failed: {exc}"
    agent_cfg = cfg.get("agent")
    workspace_text = ""
    if isinstance(agent_cfg, dict):
        workspace_text = str(agent_cfg.get("workspace", "")).strip()
    workspace = Path(workspace_text).expanduser() if workspace_text else (config_path.parent / "workspace")
    return CronService(cron_store_path(workspace)), None


def _format_schedule(job: Any) -> str:
    return format_schedule(getattr(job, "schedule", None))


def _format_ts(ms: int | None) -> str:
    return format_timestamp_ms(ms)


def cmd_cron_list(
    *,
    include_disabled: bool,
    agent: str | None,
    stdout_line: Callable[[str], None],
    resolve_target_agent_names: Callable[[str | None], tuple[list[str], str | None]],
    print_agent_output_sections: Callable[[list[tuple[str, int, str, str]]], int],
    cron_service_local: Callable[[], CronService],
    cron_service_for_agent_fn: Callable[[str], tuple[CronService | None, str | None]],
) -> int:
    target_agents, error = resolve_target_agent_names(agent)
    if error:
        stdout_line(error)
        return 1
    if target_agents:
        results: list[tuple[str, int, str, str]] = []
        for agent_name in target_agents:
            service, service_error = cron_service_for_agent_fn(agent_name)
            if service_error:
                results.append((agent_name, 1, "", service_error))
                continue
            assert service is not None
            jobs = service.list_jobs(include_disabled=include_disabled)
            lines: list[str] = []
            if not jobs:
                lines.append("No scheduled jobs.")
            else:
                lines.append("Scheduled jobs:")
                for job in jobs:
                    status = "enabled" if job.enabled else "disabled"
                    lines.append(
                        f"- {job.name} (id: {job.id}, {_format_schedule(job)}, {status}, next={_format_ts(job.state.next_run_at_ms)})"
                    )
            results.append((agent_name, 0, "\n".join(lines), ""))
        return print_agent_output_sections(results)

    service = cron_service_local()
    jobs = service.list_jobs(include_disabled=include_disabled)
    if not jobs:
        stdout_line("No scheduled jobs.")
        return 0
    stdout_line("Scheduled jobs:")
    for job in jobs:
        status = "enabled" if job.enabled else "disabled"
        stdout_line(
            f"- {job.name} (id: {job.id}, {_format_schedule(job)}, {status}, next={_format_ts(job.state.next_run_at_ms)})"
        )
    return 0


def cmd_cron_add(
    *,
    name: str,
    message: str,
    every: int | None,
    cron_expr: str | None,
    tz: str | None,
    at: str | None,
    deliver: bool,
    to: str | None,
    channel: str | None,
    stdout_line: Callable[[str], None],
    cron_service_local: Callable[[], CronService],
) -> int:
    if tz and not cron_expr:
        stdout_line("Error: --tz can only be used with --cron")
        return 1
    if deliver and not to:
        stdout_line("Error: --to is required when --deliver is set")
        return 1

    parsed, parse_error = parse_schedule_input(
        every_seconds=every,
        cron_expr=cron_expr,
        at=at,
        tz=tz,
    )
    if parse_error:
        stdout_line(f"Error: {parse_error}")
        return 1
    if parsed is None:
        stdout_line("Error: failed to parse schedule")
        return 1
    schedule = parsed.schedule
    delete_after_run = parsed.delete_after_run

    target_channel = channel or "local"
    target_to = to or "default"
    job = cron_service_local().add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=deliver,
        channel=target_channel,
        to=target_to,
        delete_after_run=delete_after_run,
    )
    stdout_line(f"Added job '{job.name}' ({job.id})")
    return 0


def cmd_cron_remove(*, job_id: str, stdout_line: Callable[[str], None], cron_service_local: Callable[[], CronService]) -> int:
    if cron_service_local().remove_job(job_id):
        stdout_line(f"Removed job {job_id}")
        return 0
    stdout_line(f"Job {job_id} not found")
    return 1


def cmd_cron_enable(
    *,
    job_id: str,
    disable: bool,
    stdout_line: Callable[[str], None],
    cron_service_local: Callable[[], CronService],
) -> int:
    job = cron_service_local().enable_job(job_id, enabled=not disable)
    if job is None:
        stdout_line(f"Job {job_id} not found")
        return 1
    state = "disabled" if disable else "enabled"
    stdout_line(f"Job '{job.name}' {state}")
    return 0


def cmd_cron_run(
    *,
    job_id: str,
    force: bool,
    stdout_line: Callable[[str], None],
    run_job_with_result: Callable[[str, bool], Any],
) -> int:
    result = run_job_with_result(job_id, force)
    if result.reason == "ok":
        stdout_line("Job executed")
        return 0
    if result.reason == "disabled":
        stdout_line(f"Job {job_id} is disabled. Use --force to run it once.")
        return 1
    if result.reason == "not_found":
        stdout_line(f"Job {job_id} not found")
        return 1
    if result.reason == "no_callback":
        stdout_line(
            "Job skipped: no executor callback is configured in this process. "
            "Run via gateway runtime to execute the agent task."
        )
        return 1
    if result.reason == "error":
        if result.error:
            stdout_line(f"Job execution failed: {result.error}")
        else:
            stdout_line(f"Job execution failed: {job_id}")
        return 1
    stdout_line(f"Job skipped: {result.reason}")
    return 1


def cmd_cron_status(
    *,
    agent: str | None,
    stdout_line: Callable[[str], None],
    resolve_target_agent_names: Callable[[str | None], tuple[list[str], str | None]],
    print_agent_output_sections: Callable[[list[tuple[str, int, str, str]]], int],
    cron_service_local: Callable[[], CronService],
    cron_service_for_agent_fn: Callable[[str], tuple[CronService | None, str | None]],
) -> int:
    target_agents, error = resolve_target_agent_names(agent)
    if error:
        stdout_line(error)
        return 1
    if target_agents:
        results: list[tuple[str, int, str, str]] = []
        for agent_name in target_agents:
            service, service_error = cron_service_for_agent_fn(agent_name)
            if service_error:
                results.append((agent_name, 1, "", service_error))
                continue
            assert service is not None
            info = service.status()
            runtime_pid = info.get("runtime_pid")
            runtime_pid_text = str(runtime_pid) if runtime_pid is not None else "-"
            line = (
                "Cron status: "
                f"local_running={info['running']}, "
                f"runtime_active={info.get('runtime_active', False)}, "
                f"runtime_pid={runtime_pid_text}, "
                f"jobs={info['jobs']}, "
                f"next_wake_at={_format_ts(info['next_wake_at_ms'])}"
            )
            results.append((agent_name, 0, line, ""))
        return print_agent_output_sections(results)

    info = cron_service_local().status()
    runtime_pid = info.get("runtime_pid")
    runtime_pid_text = str(runtime_pid) if runtime_pid is not None else "-"
    stdout_line(
        "Cron status: "
        f"local_running={info['running']}, "
        f"runtime_active={info.get('runtime_active', False)}, "
        f"runtime_pid={runtime_pid_text}, "
        f"jobs={info['jobs']}, "
        f"next_wake_at={_format_ts(info['next_wake_at_ms'])}"
    )
    return 0


def dispatch_cron_command(
    *,
    args: Any,
    parser: Any,
    stdout_line: Callable[[str], None],
    global_enabled_agent_names: Callable[[], list[str]],
    run_agent_cli_command: Callable[[str, list[str]], tuple[int, str, str]],
    cmd_cron_list_fn: Callable[[bool, str | None], int],
    cmd_cron_add_fn: Callable[[str, str, int | None, str | None, str | None, str | None, bool, str | None, str | None], int],
    cmd_cron_remove_fn: Callable[[str], int],
    cmd_cron_enable_fn: Callable[[str, bool], int],
    cmd_cron_run_fn: Callable[[str, bool], int],
    cmd_cron_status_fn: Callable[[str | None], int],
) -> int:
    raw_agent = getattr(args, "agent", None)
    selected_agent = str(raw_agent).strip() if raw_agent is not None else ""
    selected_agent = selected_agent or None
    enabled_agents = global_enabled_agent_names()
    if args.cron_command in {"add", "remove", "enable", "run"} and not selected_agent and enabled_agents:
        stdout_line(f"Error: `openpipixia cron {args.cron_command}` requires --agent in multi-agent mode.")
        return 1
    if selected_agent and enabled_agents and args.cron_command in {"add", "remove", "enable", "run"}:
        proxy_args: list[str] = ["cron", args.cron_command]
        if args.cron_command == "add":
            proxy_args.extend(["--name", args.name, "--message", args.message])
            if args.every is not None:
                proxy_args.extend(["--every", str(args.every)])
            if args.cron_expr is not None:
                proxy_args.extend(["--cron", args.cron_expr])
            if args.tz is not None:
                proxy_args.extend(["--tz", args.tz])
            if args.at is not None:
                proxy_args.extend(["--at", args.at])
            if args.deliver:
                proxy_args.append("--deliver")
            if args.to is not None:
                proxy_args.extend(["--to", args.to])
            if args.channel is not None:
                proxy_args.extend(["--channel", args.channel])
        elif args.cron_command == "remove":
            proxy_args.append(args.job_id)
        elif args.cron_command == "enable":
            proxy_args.append(args.job_id)
            if args.disable:
                proxy_args.append("--disable")
        elif args.cron_command == "run":
            proxy_args.append(args.job_id)
            if args.force:
                proxy_args.append("--force")
        code, out, err = run_agent_cli_command(selected_agent, proxy_args)
        if out.strip():
            stdout_line(out.strip())
        if err.strip():
            stdout_line(err.strip())
        return 0 if code == 0 else 1

    handlers: dict[str, Callable[[], int]] = {
        "list": lambda: cmd_cron_list_fn(args.all, selected_agent),
        "add": lambda: cmd_cron_add_fn(
            args.name,
            args.message,
            args.every,
            args.cron_expr,
            args.tz,
            args.at,
            args.deliver,
            args.to,
            args.channel,
        ),
        "remove": lambda: cmd_cron_remove_fn(args.job_id),
        "enable": lambda: cmd_cron_enable_fn(args.job_id, args.disable),
        "run": lambda: cmd_cron_run_fn(args.job_id, args.force),
        "status": lambda: cmd_cron_status_fn(selected_agent),
    }
    handler = handlers.get(args.cron_command)
    if handler is None:
        parser.print_help()
        return 2
    return handler()


def cmd_heartbeat_status(
    *,
    output_json: bool,
    agent: str | None,
    stdout_line: Callable[[str], None],
    resolve_target_agent_names: Callable[[str | None], tuple[list[str], str | None]],
    run_agent_cli_command: Callable[[str, list[str]], tuple[int, str, str]],
    print_agent_output_sections: Callable[[list[tuple[str, int, str, str]]], int],
    workspace_root: Callable[[], Path],
) -> int:
    target_agents, error = resolve_target_agent_names(agent)
    if error:
        stdout_line(error)
        return 1
    if target_agents:
        if output_json:
            merged: dict[str, Any] = {}
            failures: list[str] = []
            for agent_name in target_agents:
                code, out, err = run_agent_cli_command(agent_name, ["heartbeat", "status", "--json"])
                if code != 0:
                    failures.append(f"{agent_name}: {err.strip() or out.strip() or f'exit_code={code}'}")
                    continue
                try:
                    merged[agent_name] = json.loads(out or "{}")
                except Exception:
                    failures.append(f"{agent_name}: invalid JSON output")
            stdout_line(json.dumps(merged, ensure_ascii=False))
            if failures:
                stdout_line(f"[warn] heartbeat status failed for agents: {'; '.join(failures)}")
                return 1
            return 0
        results: list[tuple[str, int, str, str]] = []
        for agent_name in target_agents:
            code, out, err = run_agent_cli_command(agent_name, ["heartbeat", "status"])
            results.append((agent_name, code, out, err))
        return print_agent_output_sections(results)

    snapshot = read_heartbeat_status_snapshot(workspace_root())
    if output_json:
        stdout_line(json.dumps(snapshot or {}, ensure_ascii=False))
        return 0
    if not snapshot:
        stdout_line("Heartbeat status: no runtime snapshot yet.")
        return 0
    delivery = snapshot.get("last_delivery")
    delivery_kind = delivery.get("kind") if isinstance(delivery, dict) else "-"
    stdout_line(
        "Heartbeat status: "
        f"running={snapshot.get('running', False)}, "
        f"enabled={snapshot.get('enabled', False)}, "
        f"last_status={snapshot.get('last_status', '-')}, "
        f"last_reason={snapshot.get('last_reason', '-')}, "
        f"target_mode={snapshot.get('target_mode', '-')}, "
        f"last_delivery_kind={delivery_kind}"
    )
    reason_summary = _format_recent_reason_counts(snapshot.get("recent_reason_counts", {}))
    stdout_line(f"Heartbeat triggers {reason_summary}")
    stdout_line("Heartbeat trigger sources: interval=timer, exec=tool wake, cron=cron wake, manual=user action.")
    return 0


def cmd_token_stats(
    *,
    output_json: bool,
    limit: int,
    provider: str | None,
    since: str | None,
    until: str | None,
    last_hours: int | None,
    display_utc: bool,
    agent: str | None,
    stdout_line: Callable[[str], None],
    resolve_target_agent_names: Callable[[str | None], tuple[list[str], str | None]],
    print_agent_output_sections: Callable[[list[tuple[str, int, str, str]]], int],
    agent_config_path: Callable[[str], Path],
    read_token_usage_stats_fn: Callable[..., dict[str, Any]],
) -> int:
    since_ms: int | None = None
    until_ms: int | None = None
    if last_hours is not None:
        now_ms = int(time.time() * 1000)
        since_ms = now_ms - (max(1, int(last_hours)) * 3600 * 1000)
        until_ms = now_ms
    else:
        try:
            since_ms = parse_time_filter_to_epoch_ms(since)
            until_ms = parse_time_filter_to_epoch_ms(until)
        except ValueError as exc:
            stdout_line(f"Error: invalid --since/--until value ({exc})")
            return 1
    if since_ms is not None and until_ms is not None and since_ms > until_ms:
        stdout_line("Error: --since must be earlier than or equal to --until")
        return 1

    target_agents, error = resolve_target_agent_names(agent)
    if error:
        stdout_line(error)
        return 1
    if not target_agents:
        stdout_line("Error: no target agents found. Configure global_config.json or pass --agent.")
        return 1

    if output_json:
        merged: dict[str, Any] = {}
        failures: list[str] = []
        for agent_name in target_agents:
            db_path = agent_config_path(agent_name).parent / "token_usage.db"
            try:
                stats = read_token_usage_stats_fn(
                    limit=limit,
                    provider=provider or None,
                    since_ms=since_ms,
                    until_ms=until_ms,
                    db_path=db_path,
                )
            except Exception as exc:
                failures.append(f"{agent_name}: {exc}")
                continue
            merged[agent_name] = {
                "dbPath": str(db_path),
                "provider": provider or "",
                "since": since or "",
                "until": until or "",
                "lastHours": int(last_hours) if last_hours is not None else None,
                **stats,
            }
        stdout_line(json.dumps(merged, ensure_ascii=False))
        if failures:
            stdout_line(f"[warn] token stats failed for agents: {'; '.join(failures)}")
            return 1
        return 0

    results: list[tuple[str, int, str, str]] = []
    for agent_name in target_agents:
        db_path = agent_config_path(agent_name).parent / "token_usage.db"
        try:
            stats = read_token_usage_stats_fn(
                limit=limit,
                provider=provider or None,
                since_ms=since_ms,
                until_ms=until_ms,
                db_path=db_path,
            )
        except Exception as exc:
            results.append((agent_name, 1, "", f"token stats read failed: {exc}"))
            continue

        lines: list[str] = [
            (
                "Token stats: "
                f"requests={stats['requests']}, "
                f"request_tokens={stats['request_tokens']}, "
                f"response_tokens={stats['response_tokens']}, "
                f"total_tokens={stats['total_tokens']}"
            )
        ]
        if last_hours is not None:
            lines.append(f"Time range: last_hours={int(last_hours)}")
        elif since or until:
            lines.append(f"Time range: since={since or '-'}, until={until or '-'}")
        lines.append(
            "Token by modality: "
            f"request(text={stats['request_text_tokens']}, image={stats['request_image_tokens']}), "
            f"response(text={stats['response_text_tokens']}, image={stats['response_image_tokens']})"
        )
        lines.append(f"Token DB: {db_path}")
        if not stats["recent"]:
            lines.append("Recent: no records")
            results.append((agent_name, 0, "\n".join(lines), ""))
            continue
        lines.append(f"Recent records (limit={int(limit)}):")
        for row in stats["recent"]:
            response_at = _format_response_at(row.get("response_at", "-"), display_utc=display_utc)
            lines.append(
                "- "
                f"{response_at}"
                f" provider={row.get('provider', '-')}"
                f" model={row.get('model', '-')}"
                f" session={row.get('session_id', '-')}"
                f" req={row.get('request_tokens', 0)}"
                f" resp={row.get('response_tokens', 0)}"
                f" total={row.get('total_tokens', 0)}"
                f" req_img={row.get('request_image_tokens', 0)}"
                f" resp_img={row.get('response_image_tokens', 0)}"
            )
        results.append((agent_name, 0, "\n".join(lines), ""))
    return print_agent_output_sections(results)


def dispatch_token_command(
    *,
    args: Any,
    parser: Any,
    cmd_token_stats_fn: Callable[[bool, int, str | None, str | None, str | None, int | None, bool, str | None], int],
) -> int:
    raw_agent = getattr(args, "agent", None)
    selected_agent = str(raw_agent).strip() if raw_agent is not None else ""
    selected_agent = selected_agent or None
    handlers: dict[str, Callable[[], int]] = {
        "stats": lambda: cmd_token_stats_fn(
            args.output_json,
            args.limit,
            args.provider,
            args.since,
            args.until,
            args.last_hours,
            bool(getattr(args, "display_utc", False)),
            selected_agent,
        ),
    }
    handler = handlers.get(args.token_command)
    if handler is None:
        parser.print_help()
        return 2
    return handler()
