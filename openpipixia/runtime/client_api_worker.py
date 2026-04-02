"""Per-agent worker helpers for the local client API service."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any


def _emit(payload: dict[str, Any]) -> None:
    """Write one NDJSON payload to stdout."""

    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Per-agent worker for openpipixia client API.")
    parser.add_argument("action", choices=["list_sessions", "get_session", "create_session", "run"])
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--user-id", default="ppx-client-user")
    return parser.parse_args()


def _event_preview_text(event: object) -> str:
    """Build a lightweight preview string from one ADK event object."""

    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    texts: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return " ".join(texts).strip()


async def _run() -> int:
    args = _parse_args()
    config_path = Path(args.config_path).expanduser().resolve()
    if not config_path.exists():
        _emit({"type": "error", "message": f"config path not found: {config_path}"})
        return 1

    from openpipixia.core.config import bootstrap_env_from_config

    bootstrap_env_from_config(config_path)

    from google.genai import types

    from openpipixia.app.agent import root_agent
    from openpipixia.runtime.adk_utils import extract_text, merge_text_stream
    from openpipixia.runtime.message_time import inject_request_time
    from openpipixia.runtime.runner_factory import create_runner
    from openpipixia.runtime.session_service import create_session_service

    session_service = create_session_service()
    app_name = root_agent.name

    if args.action == "create_session":
        session = await session_service.create_session(
            app_name=app_name,
            user_id=args.user_id,
            session_id=args.session_id or None,
        )
        _emit(
            {
                "type": "session_created",
                "session": {
                    "id": session.id,
                    "app_name": session.app_name,
                    "user_id": session.user_id,
                    "last_update_time": session.last_update_time,
                },
            }
        )
        return 0

    if args.action == "list_sessions":
        response = await session_service.list_sessions(app_name=app_name, user_id=args.user_id)
        _emit(
            {
                "type": "session_list",
                "sessions": [
                    {
                        "id": session.id,
                        "app_name": session.app_name,
                        "user_id": session.user_id,
                        "last_update_time": session.last_update_time,
                        "event_count": len(session.events),
                        "last_preview": _event_preview_text(session.events[-1]) if session.events else "",
                    }
                    for session in response.sessions
                ],
            }
        )
        return 0

    if args.action == "get_session":
        if not args.session_id:
            _emit({"type": "error", "message": "--session-id is required for get_session"})
            return 1
        session = await session_service.get_session(
            app_name=app_name,
            user_id=args.user_id,
            session_id=args.session_id,
        )
        if session is None:
            _emit({"type": "session_detail", "session": None})
            return 0
        _emit(
            {
                "type": "session_detail",
                "session": {
                    "id": session.id,
                    "app_name": session.app_name,
                    "user_id": session.user_id,
                    "last_update_time": session.last_update_time,
                    "events": [event.model_dump(mode="json") for event in session.events],
                },
            }
        )
        return 0

    if not args.session_id:
        _emit({"type": "error", "message": "--session-id is required for run"})
        return 1
    if not args.message:
        _emit({"type": "error", "message": "--message is required for run"})
        return 1

    prompt = inject_request_time(args.message, received_at=dt.datetime.now().astimezone())
    request = types.UserContent(parts=[types.Part.from_text(text=prompt)])
    runner, _service = create_runner(agent=root_agent, app_name=app_name, session_service=session_service)

    final_text = ""
    async for event in runner.run_async(user_id=args.user_id, session_id=args.session_id, new_message=request):
        payload = event.model_dump(mode="json")
        _emit({"type": "event", "event": payload})
        text = extract_text(getattr(event, "content", None))
        merged = merge_text_stream(final_text, text)
        if merged and merged != final_text:
            final_text = merged
            _emit({"type": "delta", "text": final_text})

    _emit({"type": "final", "text": final_text})
    return 0


def main() -> int:
    """Run the worker entrypoint."""

    try:
        return asyncio.run(_run())
    except Exception as exc:  # pragma: no cover - defensive worker fallback
        _emit({"type": "error", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
