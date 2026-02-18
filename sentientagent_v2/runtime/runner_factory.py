"""Runner construction helpers shared by CLI and gateway."""

from __future__ import annotations

from typing import Any

from google.adk.apps import App, ResumabilityConfig
from google.adk.runners import Runner

from .session_service import create_session_service


def create_runner(
    *,
    agent: Any,
    app_name: str,
    session_service: Any | None = None,
) -> tuple[Runner, Any]:
    """Create a runner with a shared session service contract.

    The runner is created from an ADK ``App`` so we can enable resumability.
    This is required by long-running tools that pause an invocation and later
    resume it with ``invocation_id``.
    """
    service = session_service or create_session_service()
    app = App(
        name=app_name,
        root_agent=agent,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    runner = Runner(
        app=app,
        app_name=app_name,
        session_service=service,
        auto_create_session=True,
    )
    return runner, service
