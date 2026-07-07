"""Small ADK helpers shared across CLI, gateway, and worker runtimes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any

from google.genai import types

EventHandler = Callable[[Any], Awaitable[None] | None]
TextUpdateHandler = Callable[[str, str], Awaitable[None] | None]


def extract_text(content: types.Content | None) -> str:
    """Join text parts from an ADK content payload without altering spacing."""
    if content is None or not content.parts:
        return ""
    return "".join(
        getattr(part, "text", "")
        for part in content.parts
        if getattr(part, "text", None) and not getattr(part, "thought", False)
    )


def _longest_suffix_prefix_overlap(current: str, candidate: str) -> int:
    """Return longest overlap where current suffix equals candidate prefix."""
    max_overlap = min(len(current), len(candidate))
    for size in range(max_overlap, 0, -1):
        if current.endswith(candidate[:size]):
            return size
    return 0


def merge_text_stream(current: str, new_text: str) -> str:
    """Merge streamed ADK text supporting delta chunks, snapshots, and finals."""
    candidate = new_text or ""
    if not candidate.strip():
        return current
    if not current:
        return candidate
    if candidate == current:
        return current
    # Snapshot stream: "hello" -> "hello world".
    if candidate.startswith(current):
        return candidate
    # Repeated shorter chunk after a fuller snapshot/final.
    if current.startswith(candidate):
        return current
    overlap = _longest_suffix_prefix_overlap(current, candidate)
    if overlap:
        return current + candidate[overlap:]
    return current + candidate


async def _maybe_await(value: Awaitable[None] | None) -> None:
    """Await callback results only when the callback returned an awaitable."""
    if isawaitable(value):
        await value


def _event_error_text(event: Any) -> str:
    """Return a readable ADK event error, or an empty string for normal events."""
    raw_code = getattr(event, "error_code", None)
    raw_message = getattr(event, "error_message", None)
    code = str(raw_code or "").strip()
    message = str(raw_message or "").strip()
    if not code and not message:
        return ""
    if code and message:
        return f"{code}: {message}"
    return code or message


async def run_text_async(
    runner: Any,
    *,
    default_when_empty: str | None = "",
    on_event: EventHandler | None = None,
    on_text_update: TextUpdateHandler | None = None,
    **run_kwargs: Any,
) -> str:
    """Run an ADK runner and return merged text from streamed events.

    ``on_event`` observes every raw ADK event. ``on_text_update`` observes only
    meaningful text changes and receives both the merged text and publishable
    delta for callers that stream incremental updates.
    """
    final = ""
    async for event in runner.run_async(**run_kwargs):
        if on_event is not None:
            await _maybe_await(on_event(event))
        error_text = _event_error_text(event)
        if error_text:
            raise RuntimeError(error_text)
        text = extract_text(getattr(event, "content", None))
        merged = merge_text_stream(final, text)
        if merged and merged != final and on_text_update is not None:
            delta = merged[len(final):] if final and merged.startswith(final) else merged
            if delta:
                await _maybe_await(on_text_update(merged, delta))
        final = merged

    if final:
        return final
    if default_when_empty is None:
        return ""
    return default_when_empty
