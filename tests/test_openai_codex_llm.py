"""Tests for OpenAI Codex ADK adapter."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from google.adk.models.llm_request import LlmRequest
from google.genai import types

from sentientagent_v2.openai_codex_llm import (
    OpenAICodexLlm,
    _consume_codex_events,
    _convert_llm_request,
)


class OpenAICodexLlmTests(unittest.TestCase):
    def test_convert_llm_request_with_tools_and_tool_outputs(self) -> None:
        """Adapter should map ADK content stream into Codex input items."""
        assistant_call = types.Part.from_function_call(name="search_docs", args={"q": "oauth"})
        assistant_call.function_call.id = "call_123"
        tool_output = types.Part.from_function_response(
            name="search_docs",
            response={"ok": True, "hits": 2},
        )
        tool_output.function_response.id = "call_123"

        llm_request = LlmRequest(
            model="openai-codex/gpt-5.1-codex",
            contents=[
                types.Content(role="user", parts=[types.Part.from_text(text="Find OAuth docs")]),
                types.Content(role="model", parts=[assistant_call]),
                types.Content(role="tool", parts=[tool_output]),
            ],
            config=types.GenerateContentConfig(
                system_instruction="You are helpful.",
                tools=[
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(
                                name="search_docs",
                                description="Search docs",
                                parameters_json_schema={
                                    "type": "object",
                                    "properties": {"q": {"type": "string"}},
                                    "required": ["q"],
                                },
                            )
                        ]
                    )
                ],
            ),
        )

        instructions, input_items, tools = _convert_llm_request(llm_request)

        self.assertEqual(instructions, "You are helpful.")
        self.assertEqual(input_items[0]["role"], "user")
        self.assertIn("input_text", str(input_items[0]["content"]))
        self.assertEqual(input_items[1]["type"], "function_call")
        self.assertEqual(input_items[1]["call_id"], "call_123")
        self.assertEqual(input_items[2]["type"], "function_call_output")
        self.assertEqual(input_items[2]["call_id"], "call_123")
        self.assertEqual(tools[0]["name"], "search_docs")

    def test_consume_codex_events_extracts_text_and_function_calls(self) -> None:
        """SSE event reducer should produce final text, calls and finish reason."""
        events = [
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "id": "fc_1",
                    "name": "search_docs",
                    "arguments": "",
                },
            },
            {"type": "response.output_text.delta", "delta": "hello "},
            {"type": "response.function_call_arguments.delta", "call_id": "call_1", "delta": '{"q":"oauth'},
            {"type": "response.function_call_arguments.delta", "call_id": "call_1", "delta": ' docs"}'},
            {
                "type": "response.output_item.done",
                "item": {"type": "function_call", "call_id": "call_1"},
            },
            {
                "type": "response.completed",
                "response": {"status": "completed"},
            },
        ]
        text, tool_calls, finish_reason = _consume_codex_events(events)
        self.assertEqual(text, "hello ")
        self.assertEqual(finish_reason, types.FinishReason.STOP)
        self.assertEqual(tool_calls[0].id, "call_1")
        self.assertEqual(tool_calls[0].name, "search_docs")
        self.assertEqual(tool_calls[0].arguments, {"q": "oauth docs"})

    def test_generate_content_async_success_path(self) -> None:
        """Adapter should emit one final ADK response object on successful call."""
        llm = OpenAICodexLlm(model="openai-codex/gpt-5.1-codex")
        llm_request = LlmRequest(
            model="openai-codex/gpt-5.1-codex",
            contents=[types.Content(role="user", parts=[types.Part.from_text(text="hello")])],
            config=types.GenerateContentConfig(system_instruction="system"),
        )

        fake_token = type("Token", (), {"account_id": "acc_1", "access": "tok_1"})()
        with patch("sentientagent_v2.openai_codex_llm._get_codex_token", return_value=fake_token):
            with patch(
                "sentientagent_v2.openai_codex_llm._request_codex",
                new=AsyncMock(return_value=("hello world", [], types.FinishReason.STOP)),
            ):
                async def _collect():
                    return [event async for event in llm.generate_content_async(llm_request, stream=False)]

                events = asyncio.run(_collect())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].finish_reason, types.FinishReason.STOP)
        self.assertIsNotNone(events[0].content)
        self.assertEqual(events[0].content.parts[0].text, "hello world")


if __name__ == "__main__":
    unittest.main()
