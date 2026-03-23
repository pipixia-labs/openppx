"""Minimal runtime heartbeat scheduler (interval + manual trigger)."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Awaitable, Callable

from .heartbeat_utils import resolve_heartbeat_prompt


_DURATION_RE = re.compile(r"^\s*(?P<value>\d+)\s*(?P<unit>ms|s|m|h)?\s*$", re.IGNORECASE)
_ACTIVE_TIME_RE = re.compile(r"^(?:([01]\d|2[0-3]):([0-5]\d)|24:00)$")
_UNIT_TO_MS = {
    "ms": 1,
    "s": 1000,
    "m": 60_000,
    "h": 3_600_000,
}
DEFAULT_WAKE_COALESCE_MS = 250
DEFAULT_WAKE_RETRY_MS = 1000
_RECENT_REASON_WINDOW = 10


def parse_heartbeat_every_ms(raw: str | None) -> int | None:
    """Parse heartbeat every-string into milliseconds; `None` means disabled."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    match = _DURATION_RE.match(text)
    if not match:
        return None
    value = int(match.group("value"))
    unit = (match.group("unit") or "m").lower()
    interval_ms = value * _UNIT_TO_MS[unit]
    if interval_ms <= 0:
        return None
    return interval_ms


@dataclass(slots=True)
class HeartbeatRunRequest:
    """One heartbeat execution request payload."""

    reason: str
    prompt: str


@dataclass(slots=True)
class HeartbeatRunResult:
    """One heartbeat execution result used by status/tests."""

    status: str
    reason: str
    duration_ms: int = 0
    error: str | None = None


class HeartbeatRunner:
    """Drive heartbeat runs from interval ticks and explicit manual requests."""

    def __init__(
        self,
        *,
        on_run: Callable[[HeartbeatRunRequest], Awaitable[None]],
        every: str | None = None,
        prompt: str | None = None,
        active_hours: dict[str, str] | None = None,
        is_busy: Callable[[], bool] | None = None,
        now_ms_fn: Callable[[], int] | None = None,
    ) -> None:
        self._on_run = on_run
        self._is_busy = is_busy or (lambda: False)
        self._now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        self._every_raw = every if every is not None else os.getenv("OPENPIPIXIA_HEARTBEAT_EVERY", "30m")
        self._interval_ms = parse_heartbeat_every_ms(self._every_raw)
        self._prompt = resolve_heartbeat_prompt(prompt if prompt is not None else os.getenv("OPENPIPIXIA_HEARTBEAT_PROMPT"))
        raw_active_hours = active_hours if active_hours is not None else {
            "start": os.getenv("OPENPIPIXIA_HEARTBEAT_ACTIVE_HOURS_START", "").strip(),
            "end": os.getenv("OPENPIPIXIA_HEARTBEAT_ACTIVE_HOURS_END", "").strip(),
            "timezone": os.getenv("OPENPIPIXIA_HEARTBEAT_ACTIVE_HOURS_TIMEZONE", "user").strip() or "user",
        }
        self._active_hours_start = str(raw_active_hours.get("start", "")).strip()
        self._active_hours_end = str(raw_active_hours.get("end", "")).strip()
        self._active_hours_timezone = str(raw_active_hours.get("timezone", "user")).strip() or "user"
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._wake_task: asyncio.Task[None] | None = None
        self._wake_due_at_ms: int | None = None
        self._wake_kind: str | None = None
        self._pending_reason: str | None = None
        self._pending_reason_priority = -1
        self._pending_reason_requested_at_ms = 0
        self._run_lock = asyncio.Lock()
        self._last_run_at_ms: int | None = None
        self._last_result: HeartbeatRunResult | None = None
        self._recent_reason_sources: deque[str] = deque(maxlen=_RECENT_REASON_WINDOW)

    @property
    def enabled(self) -> bool:
        return self._interval_ms is not None

    def status(self) -> dict[str, object]:
        """Return lightweight heartbeat runner status."""
        reason_counts: dict[str, int] = {}
        for source in self._recent_reason_sources:
            reason_counts[source] = reason_counts.get(source, 0) + 1
        return {
            "running": self._running,
            "enabled": self.enabled,
            "interval_ms": self._interval_ms,
            "active_hours_enabled": bool(self._active_hours_start and self._active_hours_end),
            "wake_pending": self._pending_reason is not None,
            "wake_reason": self._pending_reason,
            "last_run_at_ms": self._last_run_at_ms,
            "last_status": self._last_result.status if self._last_result else None,
            "last_reason": self._last_result.reason if self._last_result else None,
            "last_duration_ms": self._last_result.duration_ms if self._last_result else None,
            "last_error": self._last_result.error if self._last_result else None,
            "recent_reason_sources": list(self._recent_reason_sources),
            "recent_reason_counts": reason_counts,
        }

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if not self.enabled:
            return
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._wake_task is not None:
            self._wake_task.cancel()
            try:
                await self._wake_task
            except asyncio.CancelledError:
                pass
            self._wake_task = None
        self._wake_due_at_ms = None
        self._wake_kind = None
        self._pending_reason = None
        self._pending_reason_priority = -1
        self._pending_reason_requested_at_ms = 0

    async def trigger_now(self, *, reason: str = "manual") -> HeartbeatRunResult:
        """Run one heartbeat attempt immediately with caller-provided reason."""
        return await self._execute_once(reason=reason)

    def request_wake(self, *, reason: str = "manual", coalesce_ms: int = DEFAULT_WAKE_COALESCE_MS) -> None:
        """Queue one wake request with coalescing + reason-priority semantics."""
        if not self._running or not self.enabled:
            return
        requested_at_ms = self._now_ms_fn()
        normalized_reason = (reason or "").strip() or "requested"
        priority = self._reason_priority(normalized_reason)
        if self._pending_reason is None:
            self._pending_reason = normalized_reason
            self._pending_reason_priority = priority
            self._pending_reason_requested_at_ms = requested_at_ms
        else:
            if priority > self._pending_reason_priority:
                self._pending_reason = normalized_reason
                self._pending_reason_priority = priority
                self._pending_reason_requested_at_ms = requested_at_ms
            elif priority == self._pending_reason_priority and requested_at_ms >= self._pending_reason_requested_at_ms:
                self._pending_reason = normalized_reason
                self._pending_reason_requested_at_ms = requested_at_ms
        self._schedule_wake(delay_ms=max(0, int(coalesce_ms)), kind="normal")

    async def _run_loop(self) -> None:
        assert self._interval_ms is not None
        try:
            while self._running:
                await asyncio.sleep(self._interval_ms / 1000)
                if not self._running:
                    break
                self.request_wake(reason="interval", coalesce_ms=0)
        except asyncio.CancelledError:
            raise

    async def _execute_once(self, *, reason: str) -> HeartbeatRunResult:
        self._record_reason_source(reason)
        if not self.enabled:
            result = HeartbeatRunResult(status="skipped", reason="disabled")
            self._last_result = result
            return result
        if not self._within_active_hours():
            result = HeartbeatRunResult(status="skipped", reason="quiet-hours")
            self._last_result = result
            return result
        if self._is_busy():
            result = HeartbeatRunResult(status="skipped", reason="busy")
            self._last_result = result
            return result
        if self._run_lock.locked():
            result = HeartbeatRunResult(status="skipped", reason="already-running")
            self._last_result = result
            return result
        started_at_ms = self._now_ms_fn()
        async with self._run_lock:
            try:
                await self._on_run(
                    HeartbeatRunRequest(
                        reason=reason,
                        prompt=self._prompt,
                    )
                )
                finished_at_ms = self._now_ms_fn()
                result = HeartbeatRunResult(
                    status="ran",
                    reason=reason,
                    duration_ms=max(0, finished_at_ms - started_at_ms),
                )
                self._last_run_at_ms = finished_at_ms
            except Exception as exc:
                finished_at_ms = self._now_ms_fn()
                result = HeartbeatRunResult(
                    status="failed",
                    reason=reason,
                    duration_ms=max(0, finished_at_ms - started_at_ms),
                    error=str(exc),
                )
                self._last_run_at_ms = finished_at_ms
        self._last_result = result
        return result

    def _record_reason_source(self, reason: str) -> None:
        self._recent_reason_sources.append(self._reason_source(reason))

    @staticmethod
    def _reason_source(reason: str) -> str:
        normalized = (reason or "").strip().lower()
        if normalized.startswith("cron:"):
            return "cron"
        if normalized.startswith("exec"):
            return "exec"
        if normalized.startswith("hook"):
            return "hook"
        if normalized in {"manual", "interval", "retry"}:
            return normalized
        return "other"

    @staticmethod
    def _reason_priority(reason: str) -> int:
        normalized = (reason or "").strip().lower()
        if normalized == "retry":
            return 0
        if normalized == "interval":
            return 1
        if normalized.startswith("cron:"):
            return 2
        if normalized.startswith("exec"):
            return 3
        if normalized.startswith("hook"):
            return 3
        if normalized == "manual":
            return 3
        return 2

    def _schedule_wake(self, *, delay_ms: int, kind: str) -> None:
        if not self._running:
            return
        due_at_ms = self._now_ms_fn() + max(0, delay_ms)
        if self._wake_task is not None and not self._wake_task.done():
            if self._wake_kind == "retry":
                return
            if self._wake_due_at_ms is not None and self._wake_due_at_ms <= due_at_ms:
                return
            self._wake_task.cancel()
        self._wake_due_at_ms = due_at_ms
        self._wake_kind = kind
        self._wake_task = asyncio.create_task(self._run_wake_after(delay_ms=delay_ms, kind=kind))

    async def _run_wake_after(self, *, delay_ms: int, kind: str) -> None:
        retry_reason: str | None = None
        try:
            await asyncio.sleep(max(0, delay_ms) / 1000)
            if not self._running:
                return
            reason = self._pending_reason or "requested"
            self._pending_reason = None
            self._pending_reason_priority = -1
            self._pending_reason_requested_at_ms = 0
            result = await self._execute_once(reason=reason)
            if result.status == "skipped" and result.reason == "busy":
                retry_reason = reason
        except asyncio.CancelledError:
            raise
        finally:
            self._wake_due_at_ms = None
            self._wake_kind = None
            self._wake_task = None
        if retry_reason and self._running:
            # Keep retry as the only pending reason and enforce retry backoff.
            self._pending_reason = retry_reason
            self._pending_reason_priority = self._reason_priority(retry_reason)
            self._pending_reason_requested_at_ms = self._now_ms_fn()
            self._schedule_wake(delay_ms=DEFAULT_WAKE_RETRY_MS, kind="retry")

    @staticmethod
    def _parse_active_minutes(raw: str, *, allow_24: bool) -> int | None:
        text = raw.strip()
        if not text:
            return None
        if not _ACTIVE_TIME_RE.match(text):
            return None
        hours, minutes = text.split(":")
        hour = int(hours)
        minute = int(minutes)
        if hour == 24:
            if not allow_24 or minute != 0:
                return None
            return 24 * 60
        return hour * 60 + minute

    @staticmethod
    def _resolve_timezone(raw: str) -> str:
        value = raw.strip().lower()
        if not value or value == "user" or value == "local":
            return datetime.now().astimezone().tzinfo.key if hasattr(datetime.now().astimezone().tzinfo, "key") else "UTC"
        try:
            ZoneInfo(raw.strip())
            return raw.strip()
        except Exception:
            return "UTC"

    def _within_active_hours(self) -> bool:
        if not self._active_hours_start or not self._active_hours_end:
            return True
        start_min = self._parse_active_minutes(self._active_hours_start, allow_24=False)
        end_min = self._parse_active_minutes(self._active_hours_end, allow_24=True)
        if start_min is None or end_min is None:
            return True
        if start_min == end_min:
            return False

        now_ms = self._now_ms_fn()
        tz_name = self._resolve_timezone(self._active_hours_timezone)
        now = datetime.fromtimestamp(now_ms / 1000, tz=ZoneInfo(tz_name))
        current = now.hour * 60 + now.minute
        if end_min > start_min:
            return start_min <= current < end_min
        return current >= start_min or current < end_min
