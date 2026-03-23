"""Heartbeat prompt/token helpers shared by runtime heartbeat execution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal


HEARTBEAT_TOKEN = "HEARTBEAT_OK"
DEFAULT_HEARTBEAT_PROMPT = (
    "Read HEARTBEAT.md if it exists (workspace context). Follow it strictly. "
    "Do not infer or repeat old tasks from prior chats. "
    "If nothing needs attention, reply HEARTBEAT_OK."
)
DEFAULT_HEARTBEAT_ACK_MAX_CHARS = 300
StripMode = Literal["heartbeat", "message"]


@dataclass(slots=True)
class HeartbeatStripResult:
    """Normalized strip result for heartbeat token processing."""

    should_skip: bool
    text: str
    did_strip: bool


def resolve_heartbeat_prompt(raw: str | None = None) -> str:
    """Return configured heartbeat prompt or fallback default text."""
    prompt = (raw or "").strip()
    return prompt or DEFAULT_HEARTBEAT_PROMPT


def _coerce_nonnegative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(0, parsed)


def _strip_token_at_edges(raw: str) -> tuple[str, bool]:
    text = raw.strip()
    if not text:
        return "", False

    token = HEARTBEAT_TOKEN
    did_strip = False
    changed = True
    while changed:
        changed = False
        current = text.strip()
        if not current:
            break

        # Prefix token: allow only boundary/end after token.
        if current.startswith(token):
            after_index = len(token)
            if after_index == len(current) or not (current[after_index].isalnum() or current[after_index] == "_"):
                text = current[after_index:].lstrip()
                did_strip = True
                changed = True
                continue

        # Suffix token: optional trailing punctuation up to 4 chars.
        match = re.search(rf"{re.escape(token)}(?P<tail>[^\w]{{0,4}})$", current)
        if not match:
            continue
        start = match.start()
        if start > 0 and (current[start - 1].isalnum() or current[start - 1] == "_"):
            continue

        before = current[:start].rstrip()
        tail = match.group("tail").lstrip()
        text = f"{before}{tail}".rstrip() if before else ""
        did_strip = True
        changed = True

    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed, did_strip


def strip_heartbeat_token(
    raw: str | None,
    *,
    mode: StripMode = "message",
    max_ack_chars: int | str = DEFAULT_HEARTBEAT_ACK_MAX_CHARS,
) -> HeartbeatStripResult:
    """Strip edge HEARTBEAT_OK token and decide whether the payload can be skipped."""
    if not raw or not raw.strip():
        return HeartbeatStripResult(should_skip=True, text="", did_strip=False)

    stripped_raw = raw.strip()
    normalized = (
        stripped_raw.replace("&nbsp;", " ")
        .replace("&NBSP;", " ")
        .replace("<b>", " ")
        .replace("</b>", " ")
        .replace("<strong>", " ")
        .replace("</strong>", " ")
        .strip("*`~_ ")
    )

    has_token = HEARTBEAT_TOKEN in stripped_raw or HEARTBEAT_TOKEN in normalized
    if not has_token:
        return HeartbeatStripResult(should_skip=False, text=stripped_raw, did_strip=False)

    original_text, original_stripped = _strip_token_at_edges(stripped_raw)
    normalized_text, normalized_stripped = _strip_token_at_edges(normalized)
    chosen_text = original_text if original_stripped and original_text else normalized_text
    did_strip = original_stripped or normalized_stripped
    if not did_strip:
        return HeartbeatStripResult(should_skip=False, text=stripped_raw, did_strip=False)
    if not chosen_text:
        return HeartbeatStripResult(should_skip=True, text="", did_strip=True)

    if mode == "heartbeat":
        threshold = _coerce_nonnegative_int(max_ack_chars, DEFAULT_HEARTBEAT_ACK_MAX_CHARS)
        if len(chosen_text) <= threshold:
            return HeartbeatStripResult(should_skip=True, text="", did_strip=True)

    return HeartbeatStripResult(should_skip=False, text=chosen_text, did_strip=True)

