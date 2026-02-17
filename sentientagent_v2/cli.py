"""Minimal CLI helpers for sentientagent_v2."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from google.genai import types

from .channels.factory import build_channel_manager, parse_enabled_channels, validate_channel_setup
from .config import (
    bootstrap_env_from_config,
    default_config,
    get_config_path,
    load_config,
    save_config,
)
from .env_utils import env_enabled
from .runtime.adk_utils import extract_text, merge_text_stream
from .runtime.runner_factory import create_runner
from .runtime.session_service import load_session_config
from .skills import get_registry


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
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_doctor() -> int:
    issues: list[str] = []
    if shutil.which("adk") is None:
        issues.append("Missing `adk` CLI. Install with: pip install google-adk")
    provider_name = os.getenv("SENTIENTAGENT_V2_PROVIDER", "google").strip().lower() or "google"
    provider_enabled = env_enabled("SENTIENTAGENT_V2_PROVIDER_ENABLED", default=True)
    if not provider_enabled:
        issues.append("No provider is enabled. Enable one in config (e.g. providers.google.enabled=true).")
    if provider_enabled and provider_name != "google":
        issues.append(
            f"Provider '{provider_name}' is enabled in config, but runtime currently supports only 'google'."
        )
    if provider_enabled and not os.getenv("GOOGLE_API_KEY", "").strip():
        issues.append(
            "Missing Google API key. Set `providers.google.apiKey` in ~/.sentientagent_v2/config.json "
            "or export GOOGLE_API_KEY."
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

    print(f"Config file: {config_path}" + (" (found)" if config_path.exists() else " (not found)"))
    print(f"Workspace: {registry.workspace}")
    print(f"Detected skills: {skills_count}")
    print(f"Provider: {provider_name} (enabled={provider_enabled})")
    print(f"Session storage: sqlite ({session_cfg.db_url})")
    print(f"Configured channels: {', '.join(configured_channels) if configured_channels else '(none)'}")
    print(
        "Web search: "
        f"enabled={web_enabled and web_search_enabled}, "
        f"provider={web_search_provider}, "
        f"api_key={'configured' if web_search_key_configured else 'missing'}"
    )

    if issues:
        print("\nIssues:")
        for item in issues:
            print(f"- {item}")
        return 1

    print("Environment looks good.")
    return 0


def _cmd_run(passthrough_args: list[str]) -> int:
    if shutil.which("adk") is None:
        print("`adk` CLI not found. Install with: pip install google-adk")
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

    print(f"Config {state}: {saved_to}")
    print(f"Workspace ready: {workspace}")
    print("")
    print("Next steps:")
    print(f"1. Edit config: {saved_to}")
    print("2. Configure providers/channels/web sections and their `enabled` flags")
    print("3. Fill providers.google.apiKey (and channel credentials if needed)")
    print("4. Start gateway: sentientagent_v2 gateway")
    print("5. Dry run: sentientagent_v2 doctor")
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
                print(f"[doctor] {item}")
            return 1

        manager, local_channel = build_channel_manager(
            bus=bus,
            channel_names=names,
            local_writer=print,
        )
        gateway = Gateway(
            agent=root_agent,
            app_name=root_agent.name,
            bus=bus,
            channel_manager=manager,
        )
        await gateway.start()
        print(f"gateway started with channels: {', '.join(names)}")
        if interactive_local and local_channel:
            print("local interactive mode: type /quit or /exit to stop.")
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
        print(f"Error running gateway: {exc}")
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
        request = types.UserContent(parts=[types.Part.from_text(text=message)])

        final = ""
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=request):
            _debug_event(event)
            text = extract_text(getattr(event, "content", None))
            final = merge_text_stream(final, text)
        return final

    try:
        final_text = asyncio.run(_run_once())
    except Exception as exc:
        print(f"Error running agent: {exc}")
        return 1

    if not final_text:
        print("(no response)")
        return 0
    print(final_text)
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

    args = parser.parse_args(argv)
    if args.command != "onboard":
        bootstrap_env_from_config()

    if args.message:
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
    else:
        parser.print_help()
        code = 2

    raise SystemExit(code)


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
