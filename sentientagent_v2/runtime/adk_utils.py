"""Small ADK helpers shared across CLI and gateway."""

from __future__ import annotations

from google.genai import types


def extract_text(content: types.Content | None) -> str:
    """Join text parts from an ADK content payload."""
    if content is None or not content.parts:
        return ""
    chunks: list[str] = []
    for part in content.parts:
        text = getattr(part, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def merge_text_stream(current: str, new_text: str) -> str:
    """Merge a newly observed text chunk/snapshot into accumulated text."""
    candidate = (new_text or "").strip()
    if not candidate:
        return current
    if not current:
        return candidate
    # Some SDK streams emit snapshots ("hello" -> "hello world"), not deltas.
    if candidate.startswith(current):
        return candidate
    # Ignore exact repeats.
    if candidate == current:
        return current
    # Fallback for delta-like chunks.
    return f"{current}\n{candidate}"
