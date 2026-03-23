"""Runtime cron scheduler service for openpipixia."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Literal
from zoneinfo import ZoneInfo


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dt_from_ms(ms: int, tz_name: str | None) -> datetime | None:
    try:
        tz = ZoneInfo(tz_name) if tz_name else datetime.now().astimezone().tzinfo
        if tz is None:
            return None
        return datetime.fromtimestamp(ms / 1000, tz=tz)
    except Exception:
        return None


def _parse_cron_number(token: str, minimum: int, maximum: int) -> int:
    value = int(token)
    if value < minimum or value > maximum:
        raise ValueError(f"value {value} out of range [{minimum}, {maximum}]")
    return value


def _parse_cron_values(field: str, minimum: int, maximum: int, *, normalize_dow: bool = False) -> tuple[set[int], bool]:
    token = field.strip()
    if token == "*":
        return set(range(minimum, maximum + 1)), True

    values: set[int] = set()
    for chunk in token.split(","):
        part = chunk.strip()
        if not part:
            raise ValueError("empty cron field chunk")

        step = 1
        if "/" in part:
            left, step_raw = part.split("/", 1)
            step = int(step_raw)
            if step <= 0:
                raise ValueError("cron step must be > 0")
        else:
            left = part

        if left == "*":
            start = minimum
            end = maximum
        elif "-" in left:
            start_raw, end_raw = left.split("-", 1)
            start = _parse_cron_number(start_raw, minimum, maximum)
            end = _parse_cron_number(end_raw, minimum, maximum)
            if end < start:
                raise ValueError("cron range end must be >= start")
        else:
            start = _parse_cron_number(left, minimum, maximum)
            end = start

        for item in range(start, end + 1, step):
            if normalize_dow and item == 7:
                values.add(0)
            else:
                values.add(item)

    if not values:
        raise ValueError("cron field has no values")
    return values, False


def _matches_day(candidate: datetime, dom_values: set[int], dow_values: set[int], dom_any: bool, dow_any: bool) -> bool:
    dom_match = candidate.day in dom_values
    # Python: Monday=0..Sunday=6 -> Cron: Sunday=0, Monday=1, ..., Saturday=6
    cron_dow = (candidate.weekday() + 1) % 7
    dow_match = cron_dow in dow_values
    if dom_any and dow_any:
        return True
    if dom_any:
        return dow_match
    if dow_any:
        return dom_match
    return dom_match or dow_match


def _compute_next_cron_run(expr: str, now_ms: int, tz_name: str | None) -> int | None:
    parts = expr.strip().split()
    if len(parts) != 5:
        return None

    try:
        minute_values, _ = _parse_cron_values(parts[0], 0, 59)
        hour_values, _ = _parse_cron_values(parts[1], 0, 23)
        dom_values, dom_any = _parse_cron_values(parts[2], 1, 31)
        month_values, _ = _parse_cron_values(parts[3], 1, 12)
        dow_values, dow_any = _parse_cron_values(parts[4], 0, 7, normalize_dow=True)
    except Exception:
        return None

    base = _dt_from_ms(now_ms, tz_name)
    if base is None:
        return None

    candidate = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Upper bound keeps worst-case scan bounded.
    for _ in range(60 * 24 * 366 * 2):
        if (
            candidate.month in month_values
            and candidate.hour in hour_values
            and candidate.minute in minute_values
            and _matches_day(candidate, dom_values, dow_values, dom_any, dow_any)
        ):
            return int(candidate.timestamp() * 1000)
        candidate += timedelta(minutes=1)
    return None


@dataclass(slots=True)
class CronSchedule:
    """Schedule definition for a cron job."""

    kind: Literal["every", "cron", "at"]
    every_seconds: int | None = None
    cron_expr: str | None = None
    at_ms: int | None = None
    tz: str | None = None


@dataclass(slots=True)
class CronPayload:
    """Execution payload for a cron job."""

    message: str
    deliver: bool = False
    channel: str | None = None
    to: str | None = None


@dataclass(slots=True)
class CronJobState:
    """Mutable execution state for a cron job."""

    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None


@dataclass(slots=True)
class CronJob:
    """Persisted cron job."""

    id: str
    name: str
    enabled: bool
    schedule: CronSchedule
    payload: CronPayload
    state: CronJobState
    created_at_ms: int
    updated_at_ms: int
    delete_after_run: bool = False


@dataclass(slots=True)
class CronStore:
    """On-disk store model."""

    version: int = 2
    jobs: list[CronJob] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class CronRunResult:
    """Result of a manual cron job execution request."""

    executed: bool
    reason: Literal["ok", "error", "skipped", "disabled", "not_found", "no_callback"]
    error: str | None = None


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    if schedule.kind == "every":
        if schedule.every_seconds is None or schedule.every_seconds <= 0:
            return None
        return now_ms + int(schedule.every_seconds * 1000)
    if schedule.kind == "at":
        if schedule.at_ms is None:
            return None
        return schedule.at_ms if schedule.at_ms > now_ms else None
    if schedule.kind == "cron":
        if not schedule.cron_expr:
            return None
        return _compute_next_cron_run(schedule.cron_expr, now_ms, schedule.tz)
    return None


class CronService:
    """In-process scheduler with persistent local store."""

    def __init__(
        self,
        store_path: Path,
        *,
        on_job: Callable[[CronJob], Awaitable[str | None]] | None = None,
        now_ms_fn: Callable[[], int] | None = None,
        sync_poll_interval_s: float = 2.0,
    ) -> None:
        self.store_path = store_path
        self.runtime_status_path = self.store_path.parent / "cron_runtime.json"
        self.on_job = on_job
        self._now_ms_fn = now_ms_fn or _now_ms
        self._sync_poll_interval_s = max(0.2, float(sync_poll_interval_s))
        self._store: CronStore | None = None
        self._store_mtime_ns: int | None = None
        self._last_store_error: str | None = None
        self._running = False
        self._timer_task: asyncio.Task[None] | None = None

    def _now(self) -> int:
        return self._now_ms_fn()

    def _read_store_mtime_ns(self) -> int | None:
        try:
            return self.store_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def _runtime_heartbeat_ttl_ms(self) -> int:
        """Return heartbeat staleness threshold for runtime-active checks."""
        return int(max(5000, self._sync_poll_interval_s * 4000))

    def _write_runtime_heartbeat(self) -> None:
        payload = {
            "pid": os.getpid(),
            "updated_at_ms": self._now(),
            "store_path": str(self.store_path),
        }
        self.runtime_status_path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_runtime_heartbeat(self) -> dict[str, int | str] | None:
        try:
            raw = json.loads(self.runtime_status_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        pid = raw.get("pid")
        updated_at_ms = raw.get("updated_at_ms")
        store_path = raw.get("store_path")
        if not isinstance(pid, int):
            return None
        if not isinstance(updated_at_ms, int):
            return None
        if not isinstance(store_path, str):
            return None
        return {"pid": pid, "updated_at_ms": updated_at_ms, "store_path": store_path}

    def _clear_runtime_heartbeat_if_owned(self) -> None:
        heartbeat = self._read_runtime_heartbeat()
        if heartbeat is None:
            return
        if int(heartbeat.get("pid", -1)) != os.getpid():
            return
        if str(heartbeat.get("store_path", "")) != str(self.store_path):
            return
        try:
            self.runtime_status_path.unlink(missing_ok=True)
        except Exception:
            return

    def _parse_legacy_schedule(self, schedule_text: str, now_ms: int) -> tuple[CronSchedule, bool]:
        value = (schedule_text or "").strip()
        if value.startswith("every:") and value.endswith("s"):
            every_seconds = int(value.removeprefix("every:").removesuffix("s"))
            return CronSchedule(kind="every", every_seconds=every_seconds), False
        if value.startswith("cron:"):
            return CronSchedule(kind="cron", cron_expr=value.removeprefix("cron:")), False
        if value.startswith("at:"):
            dt_obj = datetime.fromisoformat(value.removeprefix("at:"))
            return CronSchedule(kind="at", at_ms=int(dt_obj.timestamp() * 1000)), True
        # Unknown format falls back to a disabled one-shot to avoid crashing startup.
        return CronSchedule(kind="at", at_ms=now_ms - 1), True

    def _deserialize_job(self, raw: dict, now_ms: int) -> CronJob | None:
        try:
            if "payload" in raw and "schedule" in raw:
                schedule_raw = raw.get("schedule") or {}
                payload_raw = raw.get("payload") or {}
                state_raw = raw.get("state") or {}
                schedule = CronSchedule(
                    kind=str(schedule_raw.get("kind", "every")),
                    every_seconds=schedule_raw.get("every_seconds"),
                    cron_expr=schedule_raw.get("cron_expr"),
                    at_ms=schedule_raw.get("at_ms"),
                    tz=schedule_raw.get("tz"),
                )
                payload = CronPayload(
                    message=str(payload_raw.get("message", "")),
                    deliver=bool(payload_raw.get("deliver", False)),
                    channel=payload_raw.get("channel"),
                    to=payload_raw.get("to"),
                )
                state = CronJobState(
                    next_run_at_ms=state_raw.get("next_run_at_ms"),
                    last_run_at_ms=state_raw.get("last_run_at_ms"),
                    last_status=state_raw.get("last_status"),
                    last_error=state_raw.get("last_error"),
                )
                created_at_ms = int(raw.get("created_at_ms", now_ms))
                updated_at_ms = int(raw.get("updated_at_ms", created_at_ms))
                return CronJob(
                    id=str(raw["id"]),
                    name=str(raw.get("name", "")),
                    enabled=bool(raw.get("enabled", True)),
                    schedule=schedule,
                    payload=payload,
                    state=state,
                    created_at_ms=created_at_ms,
                    updated_at_ms=updated_at_ms,
                    delete_after_run=bool(raw.get("delete_after_run", False)),
                )

            # Legacy v1 format compatibility.
            schedule, delete_after_run = self._parse_legacy_schedule(str(raw.get("schedule", "")), now_ms)
            created_at_ms = now_ms
            created_at = str(raw.get("created_at", "")).strip()
            if created_at:
                try:
                    created_at_ms = int(datetime.fromisoformat(created_at).timestamp() * 1000)
                except Exception:
                    created_at_ms = now_ms
            return CronJob(
                id=str(raw["id"]),
                name=str(raw.get("name", "")),
                enabled=True,
                schedule=schedule,
                payload=CronPayload(message=str(raw.get("message", ""))),
                state=CronJobState(),
                created_at_ms=created_at_ms,
                updated_at_ms=created_at_ms,
                delete_after_run=delete_after_run,
            )
        except Exception:
            return None

    def _serialize_job(self, job: CronJob) -> dict:
        return {
            "id": job.id,
            "name": job.name,
            "enabled": job.enabled,
            "schedule": {
                "kind": job.schedule.kind,
                "every_seconds": job.schedule.every_seconds,
                "cron_expr": job.schedule.cron_expr,
                "at_ms": job.schedule.at_ms,
                "tz": job.schedule.tz,
            },
            "payload": {
                "message": job.payload.message,
                "deliver": job.payload.deliver,
                "channel": job.payload.channel,
                "to": job.payload.to,
            },
            "state": {
                "next_run_at_ms": job.state.next_run_at_ms,
                "last_run_at_ms": job.state.last_run_at_ms,
                "last_status": job.state.last_status,
                "last_error": job.state.last_error,
            },
            "created_at_ms": job.created_at_ms,
            "updated_at_ms": job.updated_at_ms,
            "delete_after_run": job.delete_after_run,
        }

    def _load_store(self) -> CronStore:
        current_mtime = self._read_store_mtime_ns()
        if self._store is not None:
            if current_mtime == self._store_mtime_ns:
                return self._store

        if current_mtime is None:
            self._store = CronStore()
            self._store_mtime_ns = None
            self._last_store_error = None
            return self._store

        now_ms = self._now()
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
            self._last_store_error = None
        except Exception as exc:
            self._last_store_error = f"failed to parse cron store: {exc}"
            # Keep the last known-good in-memory state when on-disk data is temporarily broken.
            if self._store is not None:
                return self._store
            self._store = CronStore()
            # Force next read to retry parse instead of pinning a broken mtime snapshot.
            self._store_mtime_ns = None
            return self._store

        jobs: list[CronJob] = []
        if isinstance(raw, dict):
            raw_jobs = raw.get("jobs", [])
        elif isinstance(raw, list):
            raw_jobs = raw
        else:
            raw_jobs = []

        for item in raw_jobs:
            if not isinstance(item, dict):
                continue
            parsed = self._deserialize_job(item, now_ms)
            if parsed is not None:
                jobs.append(parsed)

        self._store = CronStore(version=2, jobs=jobs)
        self._store_mtime_ns = current_mtime
        return self._store

    def _save_store(self) -> None:
        if self._store is None:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self._store.version,
            "jobs": [self._serialize_job(job) for job in self._store.jobs],
        }
        temp_name = f".{self.store_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        temp_path = self.store_path.with_name(temp_name)
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, self.store_path)
            self._store_mtime_ns = self._read_store_mtime_ns()
            self._last_store_error = None
        except Exception as exc:
            self._last_store_error = f"failed to save cron store: {exc}"
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def _recompute_next_runs(self) -> None:
        store = self._load_store()
        now_ms = self._now()
        for job in store.jobs:
            if not job.enabled:
                job.state.next_run_at_ms = None
                continue
            if job.schedule.kind == "every" and job.state.next_run_at_ms is not None and job.state.next_run_at_ms > now_ms:
                continue
            job.state.next_run_at_ms = _compute_next_run(job.schedule, now_ms)

    def _next_wake_ms(self) -> int | None:
        store = self._load_store()
        pending = [j.state.next_run_at_ms for j in store.jobs if j.enabled and j.state.next_run_at_ms is not None]
        return min(pending) if pending else None

    def _arm_timer(self) -> None:
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None

        if not self._running:
            return
        wake_at = self._next_wake_ms()
        if wake_at is None:
            delay_s = self._sync_poll_interval_s
        else:
            delay_s = min(max(0, wake_at - self._now()) / 1000, self._sync_poll_interval_s)

        async def _timer() -> None:
            await asyncio.sleep(delay_s)
            if self._running:
                await self.tick_once()

        self._timer_task = asyncio.create_task(_timer())

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._load_store()
        self._recompute_next_runs()
        self._save_store()
        self._write_runtime_heartbeat()
        self._arm_timer()

    def stop(self) -> None:
        self._running = False
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None
        self._clear_runtime_heartbeat_if_owned()

    async def _execute_job(self, job: CronJob) -> CronRunResult:
        started_at = self._now()
        result = CronRunResult(executed=False, reason="skipped")
        try:
            if self.on_job is None:
                no_callback_error = "no on_job callback configured"
                job.state.last_status = "skipped"
                job.state.last_error = no_callback_error
                result = CronRunResult(executed=False, reason="no_callback", error=no_callback_error)
            else:
                await self.on_job(job)
                job.state.last_status = "ok"
                job.state.last_error = None
                result = CronRunResult(executed=True, reason="ok")
        except Exception as exc:  # pragma: no cover - callback failure path
            job.state.last_status = "error"
            job.state.last_error = str(exc)
            result = CronRunResult(executed=False, reason="error", error=str(exc))

        job.state.last_run_at_ms = started_at
        job.updated_at_ms = self._now()

        if job.schedule.kind == "at":
            if job.delete_after_run:
                store = self._load_store()
                store.jobs = [item for item in store.jobs if item.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
            return result

        if job.enabled:
            job.state.next_run_at_ms = _compute_next_run(job.schedule, self._now())
        return result

    async def tick_once(self) -> int:
        store = self._load_store()
        now_ms = self._now()
        due_jobs = [
            job
            for job in list(store.jobs)
            if job.enabled and job.state.next_run_at_ms is not None and job.state.next_run_at_ms <= now_ms
        ]
        for job in due_jobs:
            await self._execute_job(job)
        self._save_store()
        if self._running:
            self._write_runtime_heartbeat()
        self._arm_timer()
        return len(due_jobs)

    def list_jobs(self, *, include_disabled: bool = False) -> list[CronJob]:
        store = self._load_store()
        jobs = store.jobs if include_disabled else [job for job in store.jobs if job.enabled]
        return sorted(jobs, key=lambda item: item.state.next_run_at_ms if item.state.next_run_at_ms is not None else 10**18)

    def add_job(
        self,
        *,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        delete_after_run: bool = False,
    ) -> CronJob:
        store = self._load_store()
        now_ms = self._now()
        job = CronJob(
            id=uuid.uuid4().hex[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(message=message, deliver=deliver, channel=channel, to=to),
            state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now_ms)),
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
            delete_after_run=delete_after_run,
        )
        store.jobs.append(job)
        self._save_store()
        self._arm_timer()
        return job

    def remove_job(self, job_id: str) -> bool:
        store = self._load_store()
        before = len(store.jobs)
        store.jobs = [job for job in store.jobs if job.id != job_id]
        removed = len(store.jobs) < before
        if removed:
            self._save_store()
            self._arm_timer()
        return removed

    def enable_job(self, job_id: str, *, enabled: bool = True) -> CronJob | None:
        store = self._load_store()
        for job in store.jobs:
            if job.id != job_id:
                continue
            job.enabled = enabled
            job.updated_at_ms = self._now()
            if enabled:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, self._now())
            else:
                job.state.next_run_at_ms = None
            self._save_store()
            self._arm_timer()
            return job
        return None

    async def run_job(self, job_id: str, *, force: bool = False) -> bool:
        """Run a job once and return True only when execution actually happened."""
        result = await self.run_job_with_result(job_id, force=force)
        return result.executed

    async def run_job_with_result(self, job_id: str, *, force: bool = False) -> CronRunResult:
        """Run a job once and return detailed execution result."""
        store = self._load_store()
        for job in list(store.jobs):
            if job.id != job_id:
                continue
            if not job.enabled and not force:
                return CronRunResult(executed=False, reason="disabled")
            result = await self._execute_job(job)
            self._save_store()
            self._arm_timer()
            return result
        return CronRunResult(executed=False, reason="not_found")

    def status(self) -> dict[str, int | bool | None]:
        store = self._load_store()
        heartbeat = self._read_runtime_heartbeat()
        runtime_active = False
        runtime_pid: int | None = None
        runtime_last_seen_at_ms: int | None = None
        if heartbeat is not None:
            store_path = str(heartbeat.get("store_path", ""))
            updated_at_ms = int(heartbeat.get("updated_at_ms", 0))
            runtime_last_seen_at_ms = updated_at_ms
            if store_path == str(self.store_path) and (self._now() - updated_at_ms) <= self._runtime_heartbeat_ttl_ms():
                runtime_active = True
                runtime_pid = int(heartbeat.get("pid", 0))
        return {
            "running": self._running,
            "runtime_active": runtime_active,
            "runtime_pid": runtime_pid,
            "runtime_last_seen_at_ms": runtime_last_seen_at_ms,
            "store_error": self._last_store_error,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._next_wake_ms(),
        }
