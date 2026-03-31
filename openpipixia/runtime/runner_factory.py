"""Runner construction helpers shared by CLI and gateway."""

from __future__ import annotations

import os
from typing import Any

from google.adk.apps.app import App, EventsCompactionConfig, ResumabilityConfig
from google.adk.runners import Runner

from .memory_service import create_memory_service
from .session_service import create_session_service
from .step_events import OpenPpxStepEventPlugin


def _parse_enabled(raw: str | None, *, default: bool) -> bool:
    """Parse common truthy/falsey env values with a deterministic fallback."""
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if not normalized:
        return default
    return normalized not in {"0", "false", "off", "no"}


def _parse_non_negative_int(raw: str | None, *, default: int) -> int:
    """Parse a non-negative integer with a fallback value."""
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except Exception:
        return default
    return value if value >= 0 else default


def _parse_positive_int(raw: str | None) -> int | None:
    """Parse a strictly positive integer from env input."""
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        value = int(stripped)
    except Exception:
        return None
    return value if value > 0 else None


def _build_events_compaction_config() -> EventsCompactionConfig | None:
    """Build ADK events compaction config from environment variables."""
    enabled = _parse_enabled(
        os.getenv("OPENPIPIXIA_COMPACTION_ENABLED"),
        default=True,
    )
    if not enabled:
        return None

    interval = _parse_non_negative_int(
        os.getenv("OPENPIPIXIA_COMPACTION_INTERVAL"),
        default=8,
    )
    overlap = _parse_non_negative_int(
        os.getenv("OPENPIPIXIA_COMPACTION_OVERLAP"),
        default=1,
    )

    kwargs: dict[str, Any] = {
        "compaction_interval": max(1, interval),
        "overlap_size": overlap,
    }

    token_threshold = _parse_positive_int(os.getenv("OPENPIPIXIA_COMPACTION_TOKEN_THRESHOLD"))
    event_retention_size = _parse_non_negative_int(
        os.getenv("OPENPIPIXIA_COMPACTION_EVENT_RETENTION"),
        default=-1,
    )
    if event_retention_size < 0:
        event_retention_size = None

    # ADK requires token_threshold and event_retention_size to be configured as
    # a pair; ignore partial input to avoid startup failure.
    if token_threshold is not None and event_retention_size is not None:
        kwargs["token_threshold"] = token_threshold
        kwargs["event_retention_size"] = event_retention_size

    return EventsCompactionConfig(**kwargs)


def create_runner(
    *,
    agent: Any,
    app_name: str,
    session_service: Any | None = None,
    memory_service: Any | None = None,
) -> tuple[Runner, Any]:
    """Create a runner with a shared session service contract.

    The runner is created from an ADK ``App`` so we can enable resumability and
    context compaction with project-level defaults.

    Memory is process-shared by default (single runner instance), so the same
    ``user_id`` can retrieve long-term memory across multiple sessions while
    preserving user isolation in ADK memory scope.
    """
    service = session_service or create_session_service()
    memory = memory_service if memory_service is not None else create_memory_service()
    app = App(
        name=app_name,
        root_agent=agent,
        resumability_config=ResumabilityConfig(is_resumable=True),
        events_compaction_config=_build_events_compaction_config(),
        plugins=[OpenPpxStepEventPlugin()],
    )
    runner = Runner(
        app=app,
        app_name=app_name,
        session_service=service,
        memory_service=memory,
        auto_create_session=True,
    )
    return runner, service
