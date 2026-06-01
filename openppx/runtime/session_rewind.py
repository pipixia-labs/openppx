"""Helpers for ADK-native session rewind operations."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any


class SessionRewindError(ValueError):
    """Raised when a session cannot be rewound from user-facing input."""


@dataclass(frozen=True, slots=True)
class RewindTarget:
    """Resolved ADK invocation target for one rewind operation."""

    invocation_id: str
    explicit: bool
    visible_event_count: int


def normalize_rewind_selector(selector: str | None) -> str | None:
    """Normalize a user-provided rewind selector into an invocation id or latest-target request."""
    value = (selector or "").strip()
    if not value or value.lower() == "last":
        return None
    return value


def _rewind_before_invocation_id(event: Any) -> str:
    actions = getattr(event, "actions", None)
    if actions is None:
        return ""
    return str(getattr(actions, "rewind_before_invocation_id", "") or "").strip()


def _event_invocation_id(event: Any) -> str:
    return str(getattr(event, "invocation_id", "") or "").strip()


def visible_events_after_rewinds(events: list[Any] | tuple[Any, ...]) -> list[Any]:
    """Return events that ADK will keep in model context after rewind markers.

    ADK stores rewind as an appended event instead of deleting history. Its
    content builder walks events backward and hides the rewound span. This helper
    mirrors that behavior so `last` means the latest still-visible invocation,
    not merely the latest raw event before a rewind marker.
    """
    result: list[Any] = []
    index = len(events) - 1
    while index >= 0:
        event = events[index]
        rewind_target = _rewind_before_invocation_id(event)
        if rewind_target:
            for target_index in range(0, index):
                if _event_invocation_id(events[target_index]) == rewind_target:
                    index = target_index
                    break
        else:
            result.append(event)
        index -= 1
    result.reverse()
    return result


def latest_visible_invocation_id(session: Any) -> str | None:
    """Return the latest invocation id still visible after ADK rewind filtering."""
    events = list(getattr(session, "events", []) or [])
    return _latest_invocation_id(visible_events_after_rewinds(events))


def _latest_invocation_id(events: list[Any]) -> str | None:
    for event in reversed(events):
        invocation_id = _event_invocation_id(event)
        if invocation_id:
            return invocation_id
    return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def resolve_rewind_target(
    session_service: Any,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    before_invocation_id: str | None = None,
) -> RewindTarget:
    """Resolve the invocation id that should be passed to `Runner.rewind_async`."""
    explicit_invocation_id = normalize_rewind_selector(before_invocation_id)
    if explicit_invocation_id:
        return RewindTarget(
            invocation_id=explicit_invocation_id,
            explicit=True,
            visible_event_count=0,
        )

    get_session = getattr(session_service, "get_session", None)
    if not callable(get_session):
        raise SessionRewindError("Session service does not support loading sessions.")

    session = await _maybe_await(
        get_session(app_name=app_name, user_id=user_id, session_id=session_id)
    )
    if session is None:
        raise SessionRewindError(f"Session not found: {session_id}")

    events = list(getattr(session, "events", []) or [])
    visible_events = visible_events_after_rewinds(events)
    invocation_id = _latest_invocation_id(visible_events)
    if not invocation_id:
        raise SessionRewindError("No invocation is available to rewind in this session.")

    return RewindTarget(
        invocation_id=invocation_id,
        explicit=False,
        visible_event_count=len(visible_events),
    )


def render_rewind_success(target: RewindTarget) -> str:
    """Render a concise user-facing success message for one rewind operation."""
    selector = "requested invocation" if target.explicit else "latest visible invocation"
    return (
        f"Rewound conversation before {selector} `{target.invocation_id}`. "
        "Future model context will ignore the rewound ADK events. "
        "External side effects such as files, sent messages, commands, and cron jobs are not rolled back."
    )
