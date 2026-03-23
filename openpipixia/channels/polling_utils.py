"""Shared helpers for channel polling loops."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Iterable


def dedupe_stripped(items: Iterable[Any] | None) -> list[str]:
    """Normalize iterable values into deduplicated non-empty strings."""
    if items is None:
        return []
    normalized = [str(item).strip() for item in items if str(item).strip()]
    return list(dict.fromkeys(normalized))


async def cancel_background_task(task: asyncio.Task[None] | None) -> None:
    """Cancel and await one background task safely."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def run_poll_loop(
    *,
    is_running: Callable[[], bool],
    poll_once: Callable[[], Awaitable[None]],
    interval_seconds: int | float,
    logger: logging.Logger,
    error_message: str,
    retry_delay_seconds: int | float = 2,
) -> None:
    """Run a resilient polling loop with consistent cancellation/error handling."""
    while is_running():
        try:
            await poll_once()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception(error_message)
            await asyncio.sleep(max(float(retry_delay_seconds), 0))
        await asyncio.sleep(max(float(interval_seconds), 0))


def parse_json_payload(raw: str, *, error_context: str) -> Any:
    """Decode an optional JSON response body with consistent error conversion."""
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{error_context}: {exc}") from exc
