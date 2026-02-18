"""Minimal CLI helpers for sentientagent_v2."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from google.genai import types
from loguru import logger

from .channels.factory import build_channel_manager, parse_enabled_channels, validate_channel_setup
from .config import (
    bootstrap_env_from_config,
    default_config,
    get_config_path,
    load_config,
    save_config,
)
from .env_utils import env_enabled
from .provider import normalize_model_name, normalize_provider_name, provider_api_key_env, validate_provider_runtime
from .runtime.adk_utils import extract_text, merge_text_stream
from .runtime.cron_service import CronService
from .runtime.cron_schedule_parser import parse_schedule_input
from .runtime.message_time import inject_request_time
from .runtime.runner_factory import create_runner
from .runtime.session_service import load_session_config
from .security import load_security_policy
from .skills import get_registry


def _stdout_line(message: str) -> None:
    """Write one plain user-facing line to stdout (without Loguru formatting)."""
    print(message)


def _cmd_skills() -> int:
    registry = get_registry()
    payload = [
        {
            "name": info.name,
            "description": info.description,
            "source": info.source,
            "location": str(info.path),
        }
        for info in registry.list_skills()
    ]
    logger.info(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_doctor() -> int:
    issues: list[str] = []
    if shutil.which("adk") is None:
        issues.append("Missing `adk` CLI. Install with: pip install google-adk")
    provider_name = normalize_provider_name(os.getenv("SENTIENTAGENT_V2_PROVIDER"))
    provider_model = normalize_model_name(provider_name, os.getenv("SENTIENTAGENT_V2_MODEL"))
    provider_enabled = env_enabled("SENTIENTAGENT_V2_PROVIDER_ENABLED", default=True)
    provider_key_env = provider_api_key_env(provider_name)
    if not provider_enabled:
        issues.append("No provider is enabled. Enable one in config (e.g. providers.google.enabled=true).")
    else:
        provider_issue = validate_provider_runtime(provider_name)
        if provider_issue:
            issues.append(provider_issue)
        if provider_key_env and not os.getenv(provider_key_env, "").strip():
            issues.append(
                f"Missing {provider_name} API key. Set `providers.{provider_name}.apiKey` "
                f"in ~/.sentientagent_v2/config.json or export {provider_key_env}."
            )

    config_path = get_config_path()
    registry = get_registry()
    skills_count = len(registry.list_skills())
    session_cfg = load_session_config()
    configured_channels = parse_enabled_channels(None)
    channel_issues = validate_channel_setup(configured_channels)
    issues.extend(channel_issues)
    web_enabled = env_enabled("SENTIENTAGENT_V2_WEB_ENABLED", default=True)
    web_search_enabled = env_enabled("SENTIENTAGENT_V2_WEB_SEARCH_ENABLED", default=True)
    web_search_provider = os.getenv("SENTIENTAGENT_V2_WEB_SEARCH_PROVIDER", "brave").strip().lower() or "brave"
    web_search_key_configured = bool(os.getenv("BRAVE_API_KEY", "").strip())
    security_policy = load_security_policy()

    logger.debug(f"Config file: {config_path}" + (" (found)" if config_path.exists() else " (not found)"))
    logger.debug(f"Workspace: {registry.workspace}")
    logger.debug(f"Detected skills: {skills_count}")
    logger.debug(f"Provider: {provider_name} (enabled={provider_enabled}, model={provider_model})")
    logger.debug(f"Session storage: sqlite ({session_cfg.db_url})")
    logger.debug(f"Configured channels: {', '.join(configured_channels) if configured_channels else '(none)'}")
    logger.debug(
        "Web search: "
        f"enabled={web_enabled and web_search_enabled}, "
        f"provider={web_search_provider}, "
        f"api_key={'configured' if web_search_key_configured else 'missing'}"
    )
    logger.debug(
        "Security: "
        f"restrict_to_workspace={security_policy.restrict_to_workspace}, "
        f"allow_exec={security_policy.allow_exec}, "
        f"allow_network={security_policy.allow_network}, "
        f"exec_allowlist={list(security_policy.exec_allowlist)}"
    )

    if issues:
        logger.info("Issues:")
        for item in issues:
            logger.info(f"- {item}")
        return 1

    logger.info("Environment looks good.")
    return 0


def _cmd_run(passthrough_args: list[str]) -> int:
    if shutil.which("adk") is None:
        logger.info("`adk` CLI not found. Install with: pip install google-adk")
        return 1

    agent_dir = Path(__file__).parent.resolve()
    cmd = ["adk", "run", str(agent_dir), *passthrough_args]
    return subprocess.call(cmd)


def _cmd_onboard(force: bool) -> int:
    config_path = get_config_path()
    existed = config_path.exists()

    if force or not existed:
        config = default_config()
        saved_to = save_config(config, config_path=config_path)
        state = "reset to defaults" if force and existed else "created"
    else:
        # Refresh while preserving existing values.
        config = load_config(config_path=config_path)
        saved_to = save_config(config, config_path=config_path)
        state = "refreshed"

    workspace = Path(str(config.get("agent", {}).get("workspace", ""))).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "skills").mkdir(parents=True, exist_ok=True)

    logger.info(f"Config {state}: {saved_to}")
    logger.info(f"Workspace ready: {workspace}")
    logger.info("Next steps:")
    logger.info(f"1. Edit config: {saved_to}")
    logger.info("2. Configure providers/channels/web sections and their `enabled` flags")
    logger.info("3. Fill providers.<provider>.apiKey for the enabled provider (and channel credentials if needed)")
    logger.info("4. Start gateway: sentientagent_v2 gateway")
    logger.info("5. Dry run: sentientagent_v2 doctor")
    return 0


def _cmd_gateway_local(sender_id: str, chat_id: str) -> int:
    return _cmd_gateway(channels="local", sender_id=sender_id, chat_id=chat_id, interactive_local=True)


def _cmd_gateway(
    *,
    channels: str | None,
    sender_id: str,
    chat_id: str,
    interactive_local: bool,
) -> int:
    from .agent import root_agent
    from .bus.queue import MessageBus
    from .gateway import Gateway

    async def _run() -> int:
        bus = MessageBus()
        names = parse_enabled_channels(channels)
        issues = validate_channel_setup(names)
        if issues:
            for item in issues:
                logger.info(f"[doctor] {item}")
            return 1

        manager, local_channel = build_channel_manager(
            bus=bus,
            channel_names=names,
            local_writer=logger.info,
        )
        gateway = Gateway(
            agent=root_agent,
            app_name=root_agent.name,
            bus=bus,
            channel_manager=manager,
        )
        await gateway.start()
        logger.info(f"gateway started with channels: {', '.join(names)}")
        if interactive_local and local_channel:
            logger.info("local interactive mode: type /quit or /exit to stop.")
        try:
            while True:
                if interactive_local and local_channel:
                    try:
                        line = await asyncio.to_thread(input, "> ")
                    except EOFError:
                        break
                    text = line.strip()
                    if not text:
                        continue
                    if text in {"/quit", "/exit"}:
                        break
                    await local_channel.ingest_text(text, chat_id=chat_id, sender_id=sender_id)
                    continue
                await asyncio.sleep(3600)
        finally:
            await gateway.stop()
        return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.info(f"Error running gateway: {exc}")
        return 1


def _cmd_message(message: str, user_id: str, session_id: str) -> int:
    from .agent import root_agent

    _debug(
        "llm.request",
        {
            "user_id": user_id,
            "session_id": session_id,
            "message": message,
            "model": getattr(root_agent, "model", None),
            "tools": [getattr(t, "__name__", str(t)) for t in getattr(root_agent, "tools", [])],
        },
    )

    async def _run_once() -> str:
        app_name = root_agent.name
        runner, _ = create_runner(agent=root_agent, app_name=app_name)
        prompt = inject_request_time(message, received_at=dt.datetime.now().astimezone())
        request = types.UserContent(parts=[types.Part.from_text(text=prompt)])

        final = ""
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=request):
            _debug_event(event)
            text = extract_text(getattr(event, "content", None))
            final = merge_text_stream(final, text)
        return final

    try:
        final_text = asyncio.run(_run_once())
    except Exception as exc:
        logger.info(f"Error running agent: {exc}")
        return 1

    if not final_text:
        logger.info("(no response)")
        return 0
    logger.info(final_text)
    return 0


def _cron_service() -> CronService:
    workspace = load_security_policy().workspace_root
    store_path = workspace / ".sentientagent_v2" / "cron_jobs.json"
    return CronService(store_path)


def _format_schedule(job) -> str:
    schedule = job.schedule
    if schedule.kind == "every":
        return f"every:{schedule.every_seconds}s"
    if schedule.kind == "cron":
        return f"cron:{schedule.cron_expr} ({schedule.tz})" if schedule.tz else f"cron:{schedule.cron_expr}"
    if schedule.kind == "at":
        if schedule.at_ms is None:
            return "at:unknown"
        return f"at:{dt.datetime.fromtimestamp(schedule.at_ms / 1000).isoformat(timespec='seconds')}"
    return schedule.kind


def _format_ts(ms: int | None) -> str:
    if ms is None:
        return "-"
    return dt.datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")


def _cmd_cron_list(*, include_disabled: bool) -> int:
    service = _cron_service()
    jobs = service.list_jobs(include_disabled=include_disabled)
    if not jobs:
        _stdout_line("No scheduled jobs.")
        return 0
    _stdout_line("Scheduled jobs:")
    for job in jobs:
        status = "enabled" if job.enabled else "disabled"
        _stdout_line(
            f"- {job.name} (id: {job.id}, {_format_schedule(job)}, {status}, next={_format_ts(job.state.next_run_at_ms)})"
        )
    return 0


def _cmd_cron_add(
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
) -> int:
    if tz and not cron_expr:
        logger.info("Error: --tz can only be used with --cron")
        return 1
    if deliver and not to:
        logger.info("Error: --to is required when --deliver is set")
        return 1

    parsed, parse_error = parse_schedule_input(
        every_seconds=every,
        cron_expr=cron_expr,
        at=at,
        tz=tz,
    )
    if parse_error:
        logger.info(f"Error: {parse_error}")
        return 1
    if parsed is None:  # pragma: no cover - defensive fallback
        logger.info("Error: failed to parse schedule")
        return 1
    schedule = parsed.schedule
    delete_after_run = parsed.delete_after_run

    target_channel = channel or "local"
    target_to = to or "default"
    job = _cron_service().add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=deliver,
        channel=target_channel,
        to=target_to,
        delete_after_run=delete_after_run,
    )
    logger.info(f"Added job '{job.name}' ({job.id})")
    return 0


def _cmd_cron_remove(job_id: str) -> int:
    if _cron_service().remove_job(job_id):
        logger.info(f"Removed job {job_id}")
        return 0
    logger.info(f"Job {job_id} not found")
    return 1


def _cmd_cron_enable(job_id: str, *, disable: bool) -> int:
    job = _cron_service().enable_job(job_id, enabled=not disable)
    if job is None:
        logger.info(f"Job {job_id} not found")
        return 1
    state = "disabled" if disable else "enabled"
    logger.info(f"Job '{job.name}' {state}")
    return 0


def _cmd_cron_run(job_id: str, *, force: bool) -> int:
    async def _run():
        return await _cron_service().run_job_with_result(job_id, force=force)

    result = asyncio.run(_run())
    if result.reason == "ok":
        logger.info("Job executed")
        return 0
    if result.reason == "disabled":
        logger.info(f"Job {job_id} is disabled. Use --force to run it once.")
        return 1
    if result.reason == "not_found":
        logger.info(f"Job {job_id} not found")
        return 1
    if result.reason == "no_callback":
        logger.info(
            "Job skipped: no executor callback is configured in this process. "
            "Run via gateway runtime to execute the agent task."
        )
        return 1
    if result.reason == "error":
        if result.error:
            logger.info(f"Job execution failed: {result.error}")
        else:
            logger.info(f"Job execution failed: {job_id}")
        return 1
    logger.info(f"Job skipped: {result.reason}")
    return 1


def _cmd_cron_status() -> int:
    info = _cron_service().status()
    runtime_pid = info.get("runtime_pid")
    runtime_pid_text = str(runtime_pid) if runtime_pid is not None else "-"
    logger.info(
        "Cron status: "
        f"local_running={info['running']}, "
        f"runtime_active={info.get('runtime_active', False)}, "
        f"runtime_pid={runtime_pid_text}, "
        f"jobs={info['jobs']}, "
        f"next_wake_at={_format_ts(info['next_wake_at_ms'])}"
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="sentientagent_v2",
        description="Lightweight skills-only agent based on Google ADK.",
    )
    parser.add_argument("-m", "--message", help="Run a single-turn request and print the response.")
    parser.add_argument("--user-id", default="local-user", help="User id for ADK session mode.")
    parser.add_argument(
        "--session-id",
        default="",
        help="Session id for ADK session mode (auto-generated if omitted).",
    )

    subparsers = parser.add_subparsers(dest="command", required=False)
    onboard_parser = subparsers.add_parser(
        "onboard",
        help="Initialize ~/.sentientagent_v2/config.json and workspace.",
    )
    onboard_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing config with defaults.",
    )
    subparsers.add_parser("skills", help="List discovered skills as JSON.")
    subparsers.add_parser("doctor", help="Check local runtime prerequisites.")

    run_parser = subparsers.add_parser("run", help="Run `adk run` for this agent.")
    run_parser.add_argument("adk_args", nargs=argparse.REMAINDER, help="Extra args passed to adk run.")
    gateway_parser = subparsers.add_parser(
        "gateway-local",
        help="Run minimal local channel gateway (bus + runner + stdio).",
    )
    gateway_parser.add_argument("--sender-id", default="local-user", help="Sender id used for inbound messages.")
    gateway_parser.add_argument("--chat-id", default="terminal", help="Chat id used for inbound messages.")
    gateway_parser = subparsers.add_parser(
        "gateway",
        help="Run gateway using env/CLI channels (e.g. feishu).",
    )
    gateway_parser.add_argument(
        "--channels",
        default=None,
        help="Comma-separated channels. Defaults to SENTIENTAGENT_V2_CHANNELS or 'local'.",
    )
    gateway_parser.add_argument("--sender-id", default="local-user", help="Sender id for local interactive mode.")
    gateway_parser.add_argument("--chat-id", default="terminal", help="Chat id for local interactive mode.")
    gateway_parser.add_argument(
        "--interactive-local",
        action="store_true",
        help="Enable terminal input loop when local channel is enabled.",
    )
    cron_parser = subparsers.add_parser("cron", help="Manage scheduled tasks.")
    cron_subparsers = cron_parser.add_subparsers(dest="cron_command", required=True)

    cron_list_parser = cron_subparsers.add_parser("list", help="List scheduled cron jobs.")
    cron_list_parser.add_argument("--all", action="store_true", help="Include disabled jobs.")

    cron_add_parser = cron_subparsers.add_parser("add", help="Add a cron job.")
    cron_add_parser.add_argument("--name", required=True, help="Job name.")
    cron_add_parser.add_argument("--message", required=True, help="Task message sent to agent.")
    cron_add_parser.add_argument("--every", type=int, default=None, help="Run every N seconds.")
    cron_add_parser.add_argument("--cron", dest="cron_expr", default=None, help="Cron expression.")
    cron_add_parser.add_argument("--tz", default=None, help="IANA timezone (with --cron).")
    cron_add_parser.add_argument("--at", default=None, help="Run once at ISO datetime.")
    cron_add_parser.add_argument("--deliver", action="store_true", help="Deliver response to channel.")
    cron_add_parser.add_argument("--to", default=None, help="Recipient id for delivery.")
    cron_add_parser.add_argument("--channel", default=None, help="Target channel for delivery.")

    cron_remove_parser = cron_subparsers.add_parser("remove", help="Remove a cron job.")
    cron_remove_parser.add_argument("job_id", help="Cron job id.")

    cron_enable_parser = cron_subparsers.add_parser("enable", help="Enable or disable a cron job.")
    cron_enable_parser.add_argument("job_id", help="Cron job id.")
    cron_enable_parser.add_argument("--disable", action="store_true", help="Disable instead of enable.")

    cron_run_parser = cron_subparsers.add_parser("run", help="Run a cron job immediately.")
    cron_run_parser.add_argument("job_id", help="Cron job id.")
    cron_run_parser.add_argument("--force", action="store_true", help="Run even if job is disabled.")

    cron_subparsers.add_parser("status", help="Show cron runtime status.")

    args = parser.parse_args(argv)
    if args.command != "onboard":
        bootstrap_env_from_config()

    # Global `-m/--message` is single-turn mode only when no subcommand is used.
    if args.command is None and args.message:
        sid = args.session_id or uuid.uuid4().hex[:12]
        code = _cmd_message(args.message, user_id=args.user_id, session_id=sid)
    elif args.command == "onboard":
        code = _cmd_onboard(force=args.force)
    elif args.command == "skills":
        code = _cmd_skills()
    elif args.command == "doctor":
        code = _cmd_doctor()
    elif args.command == "run":
        code = _cmd_run(args.adk_args)
    elif args.command == "gateway-local":
        code = _cmd_gateway_local(sender_id=args.sender_id, chat_id=args.chat_id)
    elif args.command == "gateway":
        code = _cmd_gateway(
            channels=args.channels,
            sender_id=args.sender_id,
            chat_id=args.chat_id,
            interactive_local=args.interactive_local,
        )
    elif args.command == "cron":
        if args.cron_command == "list":
            code = _cmd_cron_list(include_disabled=args.all)
        elif args.cron_command == "add":
            code = _cmd_cron_add(
                name=args.name,
                message=args.message,
                every=args.every,
                cron_expr=args.cron_expr,
                tz=args.tz,
                at=args.at,
                deliver=args.deliver,
                to=args.to,
                channel=args.channel,
            )
        elif args.cron_command == "remove":
            code = _cmd_cron_remove(args.job_id)
        elif args.cron_command == "enable":
            code = _cmd_cron_enable(args.job_id, disable=args.disable)
        elif args.cron_command == "run":
            code = _cmd_cron_run(args.job_id, force=args.force)
        elif args.cron_command == "status":
            code = _cmd_cron_status()
        else:
            parser.print_help()
            code = 2
    else:
        parser.print_help()
        code = 2

    raise SystemExit(code)


def _debug_enabled() -> bool:
    return env_enabled("SENTIENTAGENT_V2_DEBUG", default=False)


def _debug_body(payload: object) -> str:
    """Serialize debug payloads for stable structured log lines."""
    try:
        return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return str(payload)


def _debug(tag: str, payload: object, *, depth: int = 1) -> None:
    if not _debug_enabled():
        return
    logger.opt(depth=depth).debug("[DEBUG] {}: {}", tag, _debug_body(payload))


def _debug_event(event: object) -> None:
    if not _debug_enabled():
        return
    content = getattr(event, "content", None)
    author = getattr(event, "author", "")
    turn_complete = getattr(event, "turn_complete", None)
    finish_reason = getattr(event, "finish_reason", None)
    error_code = getattr(event, "error_code", None)
    error_message = getattr(event, "error_message", None)
    actions = getattr(event, "actions", None)
    parts_log: list[dict[str, object]] = []
    if content and getattr(content, "parts", None):
        for part in content.parts:
            row: dict[str, object] = {}
            text = getattr(part, "text", None)
            if text:
                row["text"] = text
            function_call = getattr(part, "function_call", None)
            if function_call:
                row["function_call"] = {
                    "name": getattr(function_call, "name", ""),
                    "args": getattr(function_call, "args", {}),
                }
            function_response = getattr(part, "function_response", None)
            if function_response:
                row["function_response"] = {
                    "name": getattr(function_response, "name", ""),
                    "response": getattr(function_response, "response", {}),
                }
            if row:
                parts_log.append(row)
    _debug(
        "llm.event",
        {
            "author": author,
            "turn_complete": turn_complete,
            "finish_reason": finish_reason,
            "error_code": error_code,
            "error_message": error_message,
            "actions": str(actions) if actions is not None else None,
            "parts": parts_log,
        },
    )


if __name__ == "__main__":
    main()
