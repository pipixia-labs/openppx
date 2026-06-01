"""Tests for ADK session rewind helpers."""

from __future__ import annotations

import asyncio
import types as pytypes
from unittest.mock import AsyncMock

import pytest

from openppx.runtime.session_rewind import (
    SessionRewindError,
    latest_visible_invocation_id,
    normalize_rewind_selector,
    resolve_rewind_target,
    visible_events_after_rewinds,
)


def _event(invocation_id: str, *, rewind_before: str = "") -> pytypes.SimpleNamespace:
    actions = pytypes.SimpleNamespace(rewind_before_invocation_id=rewind_before) if rewind_before else None
    return pytypes.SimpleNamespace(invocation_id=invocation_id, actions=actions)


def test_normalize_rewind_selector_treats_blank_and_last_as_latest() -> None:
    assert normalize_rewind_selector(None) is None
    assert normalize_rewind_selector("") is None
    assert normalize_rewind_selector(" last ") is None
    assert normalize_rewind_selector("inv-123") == "inv-123"


def test_visible_events_after_rewinds_matches_adk_backwards_filter() -> None:
    events = [
        _event("inv-1"),
        _event("inv-2"),
        _event("rewind-marker", rewind_before="inv-2"),
        _event("inv-3"),
    ]

    visible = visible_events_after_rewinds(events)

    assert [event.invocation_id for event in visible] == ["inv-1", "inv-3"]


def test_latest_visible_invocation_uses_effective_history_after_rewind() -> None:
    session = pytypes.SimpleNamespace(
        events=[
            _event("inv-1"),
            _event("inv-2"),
            _event("rewind-marker", rewind_before="inv-2"),
        ]
    )

    assert latest_visible_invocation_id(session) == "inv-1"


def test_resolve_rewind_target_loads_latest_visible_invocation() -> None:
    session = pytypes.SimpleNamespace(events=[_event("inv-1"), _event("inv-2")])
    service = pytypes.SimpleNamespace(get_session=AsyncMock(return_value=session))

    target = asyncio.run(
        resolve_rewind_target(
            service,
            app_name="openppx",
            user_id="u1",
            session_id="s1",
        )
    )

    assert target.invocation_id == "inv-2"
    assert target.explicit is False
    assert target.visible_event_count == 2
    service.get_session.assert_awaited_once_with(app_name="openppx", user_id="u1", session_id="s1")


def test_resolve_rewind_target_accepts_explicit_invocation_without_session_load() -> None:
    service = pytypes.SimpleNamespace(get_session=AsyncMock(side_effect=AssertionError("unused")))

    target = asyncio.run(
        resolve_rewind_target(
            service,
            app_name="openppx",
            user_id="u1",
            session_id="s1",
            before_invocation_id="inv-explicit",
        )
    )

    assert target.invocation_id == "inv-explicit"
    assert target.explicit is True
    service.get_session.assert_not_called()


def test_resolve_rewind_target_errors_when_session_has_no_invocations() -> None:
    service = pytypes.SimpleNamespace(get_session=AsyncMock(return_value=pytypes.SimpleNamespace(events=[])))

    with pytest.raises(SessionRewindError, match="No invocation"):
        asyncio.run(
            resolve_rewind_target(
                service,
                app_name="openppx",
                user_id="u1",
                session_id="s1",
            )
        )
