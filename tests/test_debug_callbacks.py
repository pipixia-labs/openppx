"""Tests for model compatibility, usage metrics, and debug trace plugins."""

from __future__ import annotations

import asyncio
import json
import os
import types as pytypes
import unittest
from unittest.mock import patch

from openppx.runtime.debug_callbacks import (
    OpenPpxDebugTracePlugin,
    OpenPpxModelCallbackState,
    OpenPpxProviderCompatibilityPlugin,
    OpenPpxUsageMetricsPlugin,
)


def _build_plugins() -> list[object]:
    state = OpenPpxModelCallbackState(profile="full")
    return [
        OpenPpxProviderCompatibilityPlugin(state=state, target_agent_name="openppx"),
        OpenPpxUsageMetricsPlugin(state=state, target_agent_name="openppx"),
        OpenPpxDebugTracePlugin(state=state, target_agent_name="openppx"),
    ]


async def _run_before_model_plugins(callback_context, llm_request, plugins: list[object] | None = None) -> list[object]:
    active_plugins = plugins or _build_plugins()
    for plugin in active_plugins:
        result = await plugin.before_model_callback(
            callback_context=callback_context,
            llm_request=llm_request,
        )
        assert result is None
    return active_plugins


async def _run_after_model_plugins(callback_context, llm_response, plugins: list[object] | None = None) -> None:
    active_plugins = plugins or _build_plugins()
    for plugin in active_plugins:
        result = await plugin.after_model_callback(
            callback_context=callback_context,
            llm_response=llm_response,
        )
        assert result is None


class DebugCallbacksTests(unittest.TestCase):
    def test_before_model_emits_request_text_when_debug_enabled(self) -> None:
        callback_context = pytypes.SimpleNamespace(
            invocation_id="inv-1",
            agent_name="openppx",
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

        with patch.dict(os.environ, {"OPENPPX_DEBUG": "1"}, clear=False):
            with patch("openppx.runtime.debug_callbacks._write_debug") as mocked_emit:
                asyncio.run(_run_before_model_plugins(callback_context, llm_request))

        mocked_emit.assert_called_once()
        tag, payload = mocked_emit.call_args.args
        self.assertEqual(tag, "llm.before_model")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("tomorrow weather in Weihai", serialized)
        self.assertIn("You are an assistant.", serialized)

    def test_before_model_omits_thought_text_when_debug_enabled(self) -> None:
        callback_context = pytypes.SimpleNamespace(
            invocation_id="inv-thought",
            agent_name="openppx",
            user_id="u-thought",
            session=pytypes.SimpleNamespace(id="s-thought"),
        )
        llm_request = pytypes.SimpleNamespace(
            model="openai/gpt-5.2",
            config=pytypes.SimpleNamespace(system_instruction=""),
            contents=[
                pytypes.SimpleNamespace(
                    role="model",
                    parts=[
                        pytypes.SimpleNamespace(text="hidden reasoning", thought=True),
                        pytypes.SimpleNamespace(text="visible answer", thought=False),
                    ],
                )
            ],
            tools_dict={},
        )

        with patch.dict(os.environ, {"OPENPPX_DEBUG": "1"}, clear=False):
            with patch("openppx.runtime.debug_callbacks._write_debug") as mocked_emit:
                asyncio.run(_run_before_model_plugins(callback_context, llm_request))

        tag, payload = mocked_emit.call_args.args
        self.assertEqual(tag, "llm.before_model")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("hidden reasoning", serialized)
        self.assertIn("visible answer", serialized)

    def test_after_model_emits_response_text_when_debug_enabled(self) -> None:
        callback_context = pytypes.SimpleNamespace(
            invocation_id="inv-2",
            agent_name="openppx",
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

        with patch.dict(os.environ, {"OPENPPX_DEBUG": "1"}, clear=False):
            with patch("openppx.runtime.debug_callbacks._write_debug") as mocked_emit:
                asyncio.run(_run_after_model_plugins(callback_context, llm_response))

        mocked_emit.assert_called_once()
        tag, payload = mocked_emit.call_args.args
        self.assertEqual(tag, "llm.after_model")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("Tomorrow is cloudy.", serialized)

    def test_after_model_does_not_truncate_when_max_chars_is_zero(self) -> None:
        callback_context = pytypes.SimpleNamespace(
            invocation_id="inv-2b",
            agent_name="openppx",
            user_id="u-2b",
            session=pytypes.SimpleNamespace(id="s-2b"),
        )
        long_text = "A" * 5000
        llm_response = pytypes.SimpleNamespace(
            finish_reason="stop",
            partial=False,
            turn_complete=True,
            error_code=None,
            error_message=None,
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text=long_text)]),
        )

        with patch.dict(
            os.environ,
            {"OPENPPX_DEBUG": "1", "OPENPPX_DEBUG_MAX_CHARS": "0"},
            clear=False,
        ):
            with patch("openppx.runtime.debug_callbacks._write_debug") as mocked_emit:
                asyncio.run(_run_after_model_plugins(callback_context, llm_response))

        mocked_emit.assert_called_once()
        _, payload = mocked_emit.call_args.args
        self.assertEqual(payload["text"], long_text)
        self.assertNotIn("truncated", payload["text"])

    def test_plugins_are_silent_when_debug_disabled(self) -> None:
        callback_context = pytypes.SimpleNamespace(
            invocation_id="inv-3",
            agent_name="openppx",
            session=pytypes.SimpleNamespace(id="s-3"),
        )
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

        with patch.dict(os.environ, {"OPENPPX_DEBUG": "0"}, clear=False):
            with patch("openppx.runtime.debug_callbacks._write_debug") as mocked_emit:
                plugins = asyncio.run(_run_before_model_plugins(callback_context, llm_request))
                asyncio.run(_run_after_model_plugins(callback_context, llm_response, plugins))

        mocked_emit.assert_not_called()

    def test_before_model_patches_missing_function_call_id_when_debug_disabled(self) -> None:
        callback_context = pytypes.SimpleNamespace(
            invocation_id="inv-fc",
            agent_name="openppx",
            session=pytypes.SimpleNamespace(id="s-4"),
        )
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

        with patch.dict(os.environ, {"OPENPPX_DEBUG": "0"}, clear=False):
            asyncio.run(_run_before_model_plugins(callback_context, llm_request))

        self.assertIsInstance(function_call.id, str)
        self.assertTrue(function_call.id.startswith("t_"))
        self.assertLessEqual(len(function_call.id), 40)

    def test_before_model_patches_missing_function_response_id_from_pending_tool_call(self) -> None:
        callback_context = pytypes.SimpleNamespace(
            invocation_id="inv-fr",
            agent_name="openppx",
            session=pytypes.SimpleNamespace(id="s-5"),
        )
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

        with patch.dict(os.environ, {"OPENPPX_DEBUG": "0"}, clear=False):
            asyncio.run(_run_before_model_plugins(callback_context, llm_request))

        self.assertIsInstance(function_call.id, str)
        self.assertLessEqual(len(function_call.id), 40)
        self.assertEqual(function_response.id, function_call.id)

    def test_before_model_normalizes_overlong_tool_ids(self) -> None:
        callback_context = pytypes.SimpleNamespace(
            invocation_id="inv-long",
            agent_name="openppx",
            session=pytypes.SimpleNamespace(id="s-6"),
        )
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

        with patch.dict(os.environ, {"OPENPPX_DEBUG": "0"}, clear=False):
            asyncio.run(_run_before_model_plugins(callback_context, llm_request))

        self.assertLessEqual(len(function_call.id), 40)
        self.assertLessEqual(len(function_response.id), 40)
        self.assertEqual(function_response.id, function_call.id)

    def test_after_model_records_token_usage_when_available(self) -> None:
        callback_context = pytypes.SimpleNamespace(
            invocation_id="inv-usage-1",
            agent_name="openppx",
            user_id="u-usage",
            session=pytypes.SimpleNamespace(id="s-usage"),
        )
        llm_request = pytypes.SimpleNamespace(
            model="gemini-2.5-pro",
            config=pytypes.SimpleNamespace(system_instruction="sys"),
            contents=[],
            tools_dict={},
        )
        usage_metadata = pytypes.SimpleNamespace(
            prompt_token_count=30,
            candidates_token_count=20,
            total_token_count=50,
            prompt_tokens_details=[
                pytypes.SimpleNamespace(modality=pytypes.SimpleNamespace(name="TEXT"), token_count=25),
                pytypes.SimpleNamespace(modality=pytypes.SimpleNamespace(name="IMAGE"), token_count=5),
            ],
            candidates_tokens_details=[
                pytypes.SimpleNamespace(modality=pytypes.SimpleNamespace(name="TEXT"), token_count=20),
            ],
        )
        llm_response = pytypes.SimpleNamespace(
            finish_reason="stop",
            partial=False,
            turn_complete=True,
            error_code=None,
            error_message=None,
            usage_metadata=usage_metadata,
            usage=None,
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="ok")]),
        )

        with patch.dict(os.environ, {"OPENPPX_DEBUG": "0", "OPENPPX_PROVIDER": "google"}, clear=False):
            plugins = asyncio.run(_run_before_model_plugins(callback_context, llm_request))
            with patch("openppx.runtime.debug_callbacks.write_token_usage_event") as mocked_write:
                asyncio.run(_run_after_model_plugins(callback_context, llm_response, plugins))

        mocked_write.assert_called_once()
        payload = mocked_write.call_args.args[0]
        self.assertEqual(payload["provider"], "google")
        self.assertEqual(payload["model"], "gemini-2.5-pro")
        self.assertEqual(payload["session_id"], "s-usage")
        self.assertEqual(payload["request_tokens"], 30)
        self.assertEqual(payload["response_tokens"], 20)
        self.assertEqual(payload["request_image_tokens"], 5)
        self.assertEqual(payload["total_tokens"], 50)

    def test_usage_state_is_isolated_by_session_for_same_invocation_id(self) -> None:
        plugins = _build_plugins()
        callback_a = pytypes.SimpleNamespace(
            invocation_id="shared-invocation",
            agent_name="openppx",
            session=pytypes.SimpleNamespace(id="session-a"),
        )
        callback_b = pytypes.SimpleNamespace(
            invocation_id="shared-invocation",
            agent_name="openppx",
            session=pytypes.SimpleNamespace(id="session-b"),
        )
        request_a = pytypes.SimpleNamespace(
            model="gemini-2.5-pro",
            config=pytypes.SimpleNamespace(system_instruction=""),
            contents=[],
            tools_dict={},
        )
        request_b = pytypes.SimpleNamespace(
            model="openai/gpt-5.2",
            config=pytypes.SimpleNamespace(system_instruction=""),
            contents=[],
            tools_dict={},
        )
        usage = pytypes.SimpleNamespace(prompt_token_count=1, candidates_token_count=2, total_token_count=3)
        response = pytypes.SimpleNamespace(
            partial=False,
            usage_metadata=usage,
            usage=None,
            content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="ok")]),
        )

        with patch.dict(os.environ, {"OPENPPX_DEBUG": "0"}, clear=False):
            asyncio.run(_run_before_model_plugins(callback_a, request_a, plugins))
            asyncio.run(_run_before_model_plugins(callback_b, request_b, plugins))
            with patch("openppx.runtime.debug_callbacks.write_token_usage_event") as mocked_write:
                asyncio.run(_run_after_model_plugins(callback_a, response, plugins))
                asyncio.run(_run_after_model_plugins(callback_b, response, plugins))

        payload_a = mocked_write.call_args_list[0].args[0]
        payload_b = mocked_write.call_args_list[1].args[0]
        self.assertEqual(payload_a["session_id"], "session-a")
        self.assertEqual(payload_a["model"], "gemini-2.5-pro")
        self.assertEqual(payload_b["session_id"], "session-b")
        self.assertEqual(payload_b["model"], "openai/gpt-5.2")

    def test_plugins_skip_non_target_agent(self) -> None:
        callback_context = pytypes.SimpleNamespace(
            invocation_id="inv-skip",
            agent_name="other_agent",
            session=pytypes.SimpleNamespace(id="s-skip"),
        )
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

        with patch.dict(os.environ, {"OPENPPX_DEBUG": "1"}, clear=False):
            with patch("openppx.runtime.debug_callbacks._write_debug") as mocked_emit:
                asyncio.run(_run_before_model_plugins(callback_context, llm_request))

        self.assertIsNone(function_call.id)
        mocked_emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
