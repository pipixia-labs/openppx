"""Tests for callback-based debug tracing."""

from __future__ import annotations

import json
import os
import types as pytypes
import unittest
from unittest.mock import patch

from sentientagent_v2.runtime.debug_callbacks import after_model_debug_callback, before_model_debug_callback


class DebugCallbacksTests(unittest.TestCase):
    def test_before_model_emits_request_text_when_debug_enabled(self) -> None:
        callback_context = pytypes.SimpleNamespace(
            invocation_id="inv-1",
            agent_name="sentientagent_v2",
            user_id="u-1",
            session=pytypes.SimpleNamespace(id="s-1"),
        )
        llm_request = pytypes.SimpleNamespace(
            model="openai/gpt-5.2",
            config=pytypes.SimpleNamespace(system_instruction="You are an assistant."),
            contents=[
                pytypes.SimpleNamespace(
                    role="user",
                    parts=[pytypes.SimpleNamespace(text="tomorrow weather in Weihai")],
                )
            ],
            tools_dict={"web_search": object(), "exec": object()},
        )

        with patch.dict(os.environ, {"SENTIENTAGENT_V2_DEBUG": "1"}, clear=False):
            with patch("sentientagent_v2.runtime.debug_callbacks._write_debug") as mocked_emit:
                result = before_model_debug_callback(callback_context, llm_request)

        self.assertIsNone(result)
        mocked_emit.assert_called_once()
        tag, payload = mocked_emit.call_args.args
        self.assertEqual(tag, "llm.before_model")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("tomorrow weather in Weihai", serialized)
        self.assertIn("You are an assistant.", serialized)

    def test_after_model_emits_response_text_when_debug_enabled(self) -> None:
        callback_context = pytypes.SimpleNamespace(
            invocation_id="inv-2",
            agent_name="sentientagent_v2",
            user_id="u-2",
            session=pytypes.SimpleNamespace(id="s-2"),
        )
        llm_response = pytypes.SimpleNamespace(
            finish_reason="stop",
            partial=False,
            turn_complete=True,
            error_code=None,
            error_message=None,
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="Tomorrow is cloudy.")]),
        )

        with patch.dict(os.environ, {"SENTIENTAGENT_V2_DEBUG": "1"}, clear=False):
            with patch("sentientagent_v2.runtime.debug_callbacks._write_debug") as mocked_emit:
                result = after_model_debug_callback(callback_context, llm_response)

        self.assertIsNone(result)
        mocked_emit.assert_called_once()
        tag, payload = mocked_emit.call_args.args
        self.assertEqual(tag, "llm.after_model")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("Tomorrow is cloudy.", serialized)

    def test_callbacks_are_silent_when_debug_disabled(self) -> None:
        callback_context = pytypes.SimpleNamespace(session=pytypes.SimpleNamespace(id="s-3"))
        llm_request = pytypes.SimpleNamespace(
            model="gemini-3-flash-preview",
            config=pytypes.SimpleNamespace(system_instruction="sys"),
            contents=[],
            tools_dict={},
        )
        llm_response = pytypes.SimpleNamespace(
            finish_reason="stop",
            partial=False,
            turn_complete=True,
            error_code=None,
            error_message=None,
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="ok")]),
        )

        with patch.dict(os.environ, {"SENTIENTAGENT_V2_DEBUG": "0"}, clear=False):
            with patch("sentientagent_v2.runtime.debug_callbacks._write_debug") as mocked_emit:
                before_model_debug_callback(callback_context, llm_request)
                after_model_debug_callback(callback_context, llm_response)

        mocked_emit.assert_not_called()

    def test_before_model_patches_missing_function_call_id_when_debug_disabled(self) -> None:
        callback_context = pytypes.SimpleNamespace(invocation_id="inv-fc", session=pytypes.SimpleNamespace(id="s-4"))
        function_call = pytypes.SimpleNamespace(id=None, name="cron", args={"action": "list"})
        llm_request = pytypes.SimpleNamespace(
            model="openai/gpt-5.2",
            config=pytypes.SimpleNamespace(system_instruction="sys"),
            contents=[
                pytypes.SimpleNamespace(
                    role="model",
                    parts=[
                        pytypes.SimpleNamespace(
                            text="",
                            function_call=function_call,
                            function_response=None,
                        )
                    ],
                )
            ],
            tools_dict={},
        )

        with patch.dict(os.environ, {"SENTIENTAGENT_V2_DEBUG": "0"}, clear=False):
            before_model_debug_callback(callback_context, llm_request)

        self.assertIsInstance(function_call.id, str)
        self.assertTrue(function_call.id.startswith("t_"))
        self.assertLessEqual(len(function_call.id), 40)

    def test_before_model_patches_missing_function_response_id_from_pending_tool_call(self) -> None:
        callback_context = pytypes.SimpleNamespace(invocation_id="inv-fr", session=pytypes.SimpleNamespace(id="s-5"))
        function_call = pytypes.SimpleNamespace(id=None, name="cron", args={"action": "list"})
        function_response = pytypes.SimpleNamespace(id=None, response={"ok": True})
        llm_request = pytypes.SimpleNamespace(
            model="openai/gpt-5.2",
            config=pytypes.SimpleNamespace(system_instruction="sys"),
            contents=[
                pytypes.SimpleNamespace(
                    role="model",
                    parts=[
                        pytypes.SimpleNamespace(
                            text="",
                            function_call=function_call,
                            function_response=None,
                        )
                    ],
                ),
                pytypes.SimpleNamespace(
                    role="tool",
                    parts=[
                        pytypes.SimpleNamespace(
                            text="",
                            function_call=None,
                            function_response=function_response,
                        )
                    ],
                ),
            ],
            tools_dict={},
        )

        with patch.dict(os.environ, {"SENTIENTAGENT_V2_DEBUG": "0"}, clear=False):
            before_model_debug_callback(callback_context, llm_request)

        self.assertIsInstance(function_call.id, str)
        self.assertLessEqual(len(function_call.id), 40)
        self.assertEqual(function_response.id, function_call.id)

    def test_before_model_normalizes_overlong_tool_ids(self) -> None:
        callback_context = pytypes.SimpleNamespace(invocation_id="inv-long", session=pytypes.SimpleNamespace(id="s-6"))
        long_id = "toolcall_" + ("x" * 72)
        function_call = pytypes.SimpleNamespace(id=long_id, name="cron", args={"action": "list"})
        function_response = pytypes.SimpleNamespace(id=long_id, response={"ok": True})
        llm_request = pytypes.SimpleNamespace(
            model="openai/gpt-5.2",
            config=pytypes.SimpleNamespace(system_instruction="sys"),
            contents=[
                pytypes.SimpleNamespace(
                    role="model",
                    parts=[
                        pytypes.SimpleNamespace(
                            text="",
                            function_call=function_call,
                            function_response=None,
                        )
                    ],
                ),
                pytypes.SimpleNamespace(
                    role="tool",
                    parts=[
                        pytypes.SimpleNamespace(
                            text="",
                            function_call=None,
                            function_response=function_response,
                        )
                    ],
                ),
            ],
            tools_dict={},
        )

        with patch.dict(os.environ, {"SENTIENTAGENT_V2_DEBUG": "0"}, clear=False):
            before_model_debug_callback(callback_context, llm_request)

        self.assertLessEqual(len(function_call.id), 40)
        self.assertLessEqual(len(function_response.id), 40)
        self.assertEqual(function_response.id, function_call.id)


if __name__ == "__main__":
    unittest.main()
