"""Helpers for ADK tool confirmation interrupts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from google.genai import types

REQUEST_CONFIRMATION_TOOL_NAME = "adk_request_confirmation"

_POSITIVE_CONFIRMATIONS = {"y", "yes", "true", "confirm", "confirmed", "approve", "approved"}
_NEGATIVE_CONFIRMATIONS = {"n", "no", "false", "reject", "rejected", "cancel", "cancelled"}


@dataclass(frozen=True, slots=True)
class ToolConfirmationRequest:
    """One pending ADK tool confirmation request."""

    confirmation_id: str
    invocation_id: str
    original_function_call_id: str
    tool_name: str
    tool_args: dict[str, Any]
    hint: str
    payload: Any | None = None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def extract_tool_confirmation_requests(event: Any) -> list[ToolConfirmationRequest]:
    """Extract ADK confirmation requests from one event."""
    long_running_ids = set(str(item) for item in (getattr(event, "long_running_tool_ids", None) or []))
    if not long_running_ids:
        return []

    get_function_calls = getattr(event, "get_function_calls", None)
    if not callable(get_function_calls):
        return []

    requests: list[ToolConfirmationRequest] = []
    invocation_id = str(getattr(event, "invocation_id", "") or "")
    for function_call in get_function_calls() or []:
        confirmation_id = str(getattr(function_call, "id", "") or "")
        if confirmation_id not in long_running_ids:
            continue
        if getattr(function_call, "name", None) != REQUEST_CONFIRMATION_TOOL_NAME:
            continue

        args = _as_dict(getattr(function_call, "args", None))
        original = _as_dict(args.get("originalFunctionCall"))
        confirmation = _as_dict(args.get("toolConfirmation"))
        original_args = _as_dict(original.get("args"))
        requests.append(
            ToolConfirmationRequest(
                confirmation_id=confirmation_id,
                invocation_id=invocation_id,
                original_function_call_id=str(original.get("id") or ""),
                tool_name=str(original.get("name") or "tool"),
                tool_args=original_args,
                hint=str(confirmation.get("hint") or ""),
                payload=confirmation.get("payload"),
            )
        )
    return requests


def parse_tool_confirmation_response(text: str) -> bool | None:
    """Parse one user text reply as approve/reject, returning None if unknown."""
    normalized = (text or "").strip().lower()
    if normalized.startswith("/confirm "):
        normalized = normalized.removeprefix("/confirm ").strip()
    elif normalized == "/confirm":
        normalized = "yes"
    elif normalized in {"/approve", "/approved"}:
        normalized = "yes"
    elif normalized in {"/reject", "/rejected", "/cancel"}:
        normalized = "no"

    if normalized in _POSITIVE_CONFIRMATIONS:
        return True
    if normalized in _NEGATIVE_CONFIRMATIONS:
        return False
    return None


def _json_preview(value: Any, *, max_chars: int = 1600) -> str:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max_chars - 16].rstrip() + "\n...<truncated>"


def render_tool_confirmation_prompt(request: ToolConfirmationRequest) -> str:
    """Render a user-facing confirmation prompt for one pending tool call."""
    hint = request.hint.strip() or f"Tool `{request.tool_name}` requires confirmation."
    return (
        f"{hint}\n\n"
        f"Tool: `{request.tool_name}`\n\n"
        "Arguments:\n"
        f"```json\n{_json_preview(request.tool_args)}\n```\n\n"
        "Reply `yes` to approve or `no` to reject."
    )


def build_tool_confirmation_response_content(
    request: ToolConfirmationRequest,
    *,
    confirmed: bool,
) -> types.Content:
    """Build the ADK function response that resumes a confirmation interrupt."""
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=request.confirmation_id,
                    name=REQUEST_CONFIRMATION_TOOL_NAME,
                    response={"confirmed": bool(confirmed)},
                )
            )
        ],
    )
