"""Tests for ADK tool confirmation helpers."""

from __future__ import annotations

from types import SimpleNamespace

from openppx.runtime.tool_confirmation import (
    REQUEST_CONFIRMATION_TOOL_NAME,
    build_tool_confirmation_response_content,
    extract_tool_confirmation_requests,
    parse_tool_confirmation_response,
    render_tool_confirmation_prompt,
)


def test_extract_tool_confirmation_request_from_adk_event() -> None:
    function_call = SimpleNamespace(
        id="confirm-1",
        name=REQUEST_CONFIRMATION_TOOL_NAME,
        args={
            "originalFunctionCall": {
                "id": "call-1",
                "name": "message",
                "args": {"content": "send this"},
            },
            "toolConfirmation": {"hint": "Approve message?"},
        },
    )
    event = SimpleNamespace(
        invocation_id="inv-1",
        long_running_tool_ids={"confirm-1"},
        get_function_calls=lambda: [function_call],
    )

    requests = extract_tool_confirmation_requests(event)

    assert len(requests) == 1
    assert requests[0].confirmation_id == "confirm-1"
    assert requests[0].invocation_id == "inv-1"
    assert requests[0].original_function_call_id == "call-1"
    assert requests[0].tool_name == "message"
    assert requests[0].tool_args == {"content": "send this"}


def test_parse_confirmation_response_supports_commands_and_plain_text() -> None:
    assert parse_tool_confirmation_response("yes") is True
    assert parse_tool_confirmation_response("/confirm yes") is True
    assert parse_tool_confirmation_response("/approve") is True
    assert parse_tool_confirmation_response("no") is False
    assert parse_tool_confirmation_response("/reject") is False
    assert parse_tool_confirmation_response("run something else") is None


def test_build_confirmation_response_content_targets_adk_interrupt() -> None:
    request = extract_tool_confirmation_requests(
        SimpleNamespace(
            invocation_id="inv-1",
            long_running_tool_ids={"confirm-1"},
            get_function_calls=lambda: [
                SimpleNamespace(
                    id="confirm-1",
                    name=REQUEST_CONFIRMATION_TOOL_NAME,
                    args={
                        "originalFunctionCall": {"id": "call-1", "name": "exec", "args": {"command": "echo ok"}},
                        "toolConfirmation": {"hint": "Approve exec?"},
                    },
                )
            ],
        )
    )[0]

    prompt = render_tool_confirmation_prompt(request)
    content = build_tool_confirmation_response_content(request, confirmed=True)

    function_response = content.parts[0].function_response
    assert "Approve exec?" in prompt
    assert function_response.id == "confirm-1"
    assert function_response.name == REQUEST_CONFIRMATION_TOOL_NAME
    assert function_response.response == {"confirmed": True}
