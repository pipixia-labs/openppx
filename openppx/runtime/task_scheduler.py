"""Deterministic maintenance scheduler for long-task runtime facts."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .task_execution import TaskController
from .task_store import TASK_TERMINAL_STATUSES, TaskDelivery, TaskDeliveryStore, TaskRun, TaskStore

logger = logging.getLogger(__name__)

DeliveryCallback = Callable[[dict[str, Any]], Awaitable[Any] | Any]
DEFAULT_STALE_LOST_AFTER_MS = 5 * 60 * 1000
DEFAULT_DELIVERY_RETRY_BASE_MS = 5_000
DEFAULT_DELIVERY_RETRY_MAX_MS = 5 * 60 * 1000


@dataclass(frozen=True, slots=True)
class SchedulerDrainResult:
    """Summary of one scheduler drain pass."""

    scanned: int
    claimed: int
    synced: int
    deliveries: int


class TaskWakeScheduler:
    """Periodically synchronize due long tasks without relying on LLM heartbeats."""

    def __init__(
        self,
        *,
        task_store: TaskStore | None = None,
        controller: TaskController | None = None,
        delivery_store: TaskDeliveryStore | None = None,
        interval_seconds: float = 1.0,
        lease_ms: int = 10_000,
        batch_size: int = 50,
        stale_lost_after_ms: int = DEFAULT_STALE_LOST_AFTER_MS,
        delivery_retry_base_ms: int = DEFAULT_DELIVERY_RETRY_BASE_MS,
        delivery_retry_max_ms: int = DEFAULT_DELIVERY_RETRY_MAX_MS,
        owner: str | None = None,
        on_delivery: DeliveryCallback | None = None,
    ) -> None:
        self.task_store = task_store or TaskStore()
        self.controller = controller or TaskController(task_store=self.task_store)
        self.delivery_store = delivery_store or TaskDeliveryStore(db_path=self.task_store.db_path)
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.lease_ms = max(100, int(lease_ms))
        self.batch_size = max(1, min(int(batch_size), 500))
        self.stale_lost_after_ms = max(0, int(stale_lost_after_ms))
        self.delivery_retry_base_ms = max(0, int(delivery_retry_base_ms))
        self.delivery_retry_max_ms = max(self.delivery_retry_base_ms, int(delivery_retry_max_ms))
        self.owner = owner or _default_owner()
        self._on_delivery = on_delivery
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._last_result = SchedulerDrainResult(scanned=0, claimed=0, synced=0, deliveries=0)

    @property
    def running(self) -> bool:
        """Return whether the scheduler background loop is active."""
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        """Return scheduler observability state."""
        return {
            "running": self.running,
            "owner": self.owner,
            "interval_seconds": self.interval_seconds,
            "lease_ms": self.lease_ms,
            "stale_lost_after_ms": self.stale_lost_after_ms,
            "delivery_retry_base_ms": self.delivery_retry_base_ms,
            "delivery_retry_max_ms": self.delivery_retry_max_ms,
            "last_result": {
                "scanned": self._last_result.scanned,
                "claimed": self._last_result.claimed,
                "synced": self._last_result.synced,
                "deliveries": self._last_result.deliveries,
            },
        }

    async def start(self) -> None:
        """Start the scheduler background loop."""
        if self.running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="openppx-task-wake-scheduler")

    async def stop(self) -> None:
        """Stop the scheduler and wait for the loop to exit."""
        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        await self._task
        self._task = None
        self._stop_event = None

    async def drain_due_tasks(self) -> SchedulerDrainResult:
        """Synchronize currently claimable process-backed tasks once."""
        now_ms = _now_ms()
        candidates = self.task_store.list_claimable_tasks(statuses=["running"], limit=self.batch_size)
        remaining = self.batch_size - len(candidates)
        if remaining > 0:
            stale_candidates = _eligible_stale_tasks(
                self.task_store,
                limit=remaining,
                now_ms=now_ms,
                stale_lost_after_ms=self.stale_lost_after_ms,
            )
            candidates.extend(stale_candidates)
        scanned = len(candidates)
        claimed = 0
        synced = 0
        deliveries = 0
        for candidate in candidates:
            claim = self.task_store.claim_task(
                candidate.task_id,
                lease_owner=self.owner,
                lease_ms=self.lease_ms,
                now_ms=now_ms,
            )
            if claim is None:
                continue
            claimed += 1
            try:
                if claim.status == "stale":
                    task = self.controller.reconcile_stale_task(
                        claim.task_id,
                        stale_lost_after_ms=self.stale_lost_after_ms,
                        now_ms=now_ms,
                    )
                elif claim.status == "running":
                    task = self.controller.sync_task(claim.task_id)
                else:
                    task = claim
                if task is None:
                    continue
                synced += 1
                if _should_deliver(task):
                    delivered = await self._record_delivery(task)
                    if delivered:
                        deliveries += 1
            finally:
                self.task_store.release_claim(
                    claim.task_id,
                    lease_owner=self.owner,
                    claim_token=claim.claim_token,
                )
        deliveries += await self._retry_due_deliveries(now_ms=now_ms, remaining_limit=self.batch_size)
        self._last_result = SchedulerDrainResult(
            scanned=scanned,
            claimed=claimed,
            synced=synced,
            deliveries=deliveries,
        )
        return self._last_result

    async def _run_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            await self.drain_due_tasks()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                continue

    async def _record_delivery(self, task: TaskRun) -> bool:
        delivery_type = f"task.{task.status}"
        payload = {
            "task_id": task.task_id,
            "kind": task.kind,
            "status": task.status,
            "title": task.title,
            "session_id": task.session_id,
            "thread_id": task.thread_id,
            "invocation_id": task.invocation_id,
            "function_call_id": task.function_call_id,
            "tool_call_id": task.tool_call_id,
            "summary": task.terminal_summary or task.progress_summary or task.last_error,
            "delivery": _delivery_target(task),
        }
        _delivery, created = self.delivery_store.record_once(
            task_id=task.task_id,
            delivery_type=delivery_type,
            payload=payload,
        )
        if not created:
            return False
        return await self._publish_delivery(_delivery)

    async def _retry_due_deliveries(self, *, now_ms: int, remaining_limit: int) -> int:
        """Retry pending/failed delivery records that are due."""
        deliveries = self.delivery_store.list_retryable_deliveries(now_ms=now_ms, limit=remaining_limit)
        delivered = 0
        for delivery in deliveries:
            if await self._publish_delivery(delivery):
                delivered += 1
        return delivered

    async def _publish_delivery(self, delivery: TaskDelivery) -> bool:
        """Publish one delivery and update durable attempt state."""
        payload = delivery.payload
        if self._on_delivery is None:
            self.delivery_store.mark_delivered(delivery.delivery_key)
            return True
        try:
            result = self._on_delivery(payload)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            retry_after_ms = self._delivery_retry_after_ms(delivery)
            self.delivery_store.mark_failed(
                delivery.delivery_key,
                error=str(exc),
                retry_after_ms=retry_after_ms,
            )
            logger.exception("Failed publishing task delivery (task_id=%s)", delivery.task_id)
            return False
        ack_payload = result if isinstance(result, dict) else {}
        provider_message_id = str(
            ack_payload.get("provider_message_id") or ack_payload.get("message_id") or ""
        )
        self.delivery_store.mark_delivered(
            delivery.delivery_key,
            ack_payload=ack_payload,
            provider_message_id=provider_message_id,
        )
        return True

    def _delivery_retry_after_ms(self, delivery: TaskDelivery) -> int:
        """Return bounded exponential retry delay for one delivery."""
        if self.delivery_retry_base_ms <= 0:
            return 0
        exponent = min(max(0, delivery.attempts), 6)
        return min(self.delivery_retry_max_ms, self.delivery_retry_base_ms * (2**exponent))


def _should_deliver(task: TaskRun) -> bool:
    """Return whether a task status should produce a once-only delivery record."""
    return task.status in TASK_TERMINAL_STATUSES or task.status in {"interrupted", "waiting_user", "waiting_approval"}


def _default_owner() -> str:
    """Return a stable owner string for this scheduler process."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _now_ms() -> int:
    """Return the current wall-clock timestamp in milliseconds."""
    return int(time.time() * 1000)


def _eligible_stale_tasks(
    task_store: TaskStore,
    *,
    limit: int,
    now_ms: int,
    stale_lost_after_ms: int,
) -> list[TaskRun]:
    """Return stale tasks whose grace period has elapsed."""
    candidates = task_store.list_claimable_tasks(statuses=["stale"], limit=limit, now_ms=now_ms)
    grace_ms = max(0, int(stale_lost_after_ms))
    return [task for task in candidates if now_ms - task.updated_at_ms >= grace_ms]


def _delivery_target(task: TaskRun) -> dict[str, str]:
    """Return the best-known channel target stored on a task."""
    raw = task.runner_payload.get("delivery")
    if not isinstance(raw, dict):
        return {}
    channel = str(raw.get("channel", "") or "").strip()
    chat_id = str(raw.get("chat_id", "") or "").strip()
    if not channel or not chat_id:
        return {}
    return {"channel": channel, "chat_id": chat_id}
