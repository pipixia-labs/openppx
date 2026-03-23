"""Tests for message-time prompt helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from openpipixia.runtime.message_time import (
    append_execution_time,
    build_current_time_line,
    inject_request_time,
)


def test_build_current_time_line_keeps_timezone() -> None:
    now = datetime(2026, 2, 18, 9, 30, tzinfo=timezone.utc)
    line = build_current_time_line(now)
    assert line == "Current time: 2026-02-18T09:30:00+00:00 (UTC)"


def test_inject_request_time_includes_guidance_and_body() -> None:
    now = datetime(2026, 2, 18, 9, 30, tzinfo=timezone.utc)
    payload = inject_request_time("hello", received_at=now)
    assert "Current request time: 2026-02-18T09:30:00+00:00 (UTC)" in payload
    assert "Use this as the reference 'now' for relative time expressions" in payload
    assert payload.endswith("\n\nhello")


def test_append_execution_time_keeps_message_body() -> None:
    now = datetime(2026, 2, 18, 9, 30, tzinfo=timezone.utc)
    payload = append_execution_time("run task", now=now)
    assert payload.startswith("run task")
    assert "Current time: 2026-02-18T09:30:00+00:00 (UTC)" in payload
