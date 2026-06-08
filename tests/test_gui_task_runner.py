"""Tests for multi-step GUI task runner."""

from __future__ import annotations

import asyncio
import tempfile
import types as pytypes
import unittest
import unittest.mock
from pathlib import Path

from openppx.gui.executor import CapturedScreen
from openppx.gui.task_runner import GuiTaskRunner, execute_gui_task
from openppx.runtime.sync_tool_proxy import SyncCancellationToken


class _FakeRuntime:
    def __init__(self) -> None:
        self._index = 0

    def capture(self) -> CapturedScreen:
        idx = self._index
        self._index += 1
        return CapturedScreen(
            base64_png=f"screen-{idx}",
            width=1920,
            height=1080,
            path=f"/tmp/task-screen-{idx}.png",
        )


class GuiTaskRunnerTests(unittest.TestCase):
    def test_build_planner_runner_uses_native_google_model_without_litellm(self) -> None:
        with unittest.mock.patch("google.adk.models.lite_llm.LiteLlm") as mocked_litellm:
            with unittest.mock.patch("google.adk.agents.LlmAgent") as mocked_agent:
                with unittest.mock.patch(
                    "openppx.runtime.runner_factory.create_runner",
                    return_value=(object(), None),
                ) as mocked_create_runner:
                    GuiTaskRunner._build_adk_planner_runner(
                        planner_model="gemini-3-flash-preview",
                        planner_api_key="google-key",
                        planner_provider="google",
                        planner_base_url=None,
                    )

        mocked_litellm.assert_not_called()
        self.assertEqual(mocked_agent.call_args.kwargs["model"], "gemini-3-flash-preview")
        self.assertEqual(mocked_create_runner.call_args.kwargs["profile"], "ephemeral")

    def test_build_planner_runner_uses_litellm_for_non_google_provider(self) -> None:
        with unittest.mock.patch("google.adk.models.lite_llm.LiteLlm") as mocked_litellm:
            mocked_litellm.return_value = object()
            with unittest.mock.patch("google.adk.agents.LlmAgent"):
                with unittest.mock.patch(
                    "openppx.runtime.runner_factory.create_runner",
                    return_value=(object(), None),
                ) as mocked_create_runner:
                    GuiTaskRunner._build_adk_planner_runner(
                        planner_model="openai/gpt-4.1-mini",
                        planner_api_key="openai-key",
                        planner_provider="openai",
                        planner_base_url=None,
                    )

        mocked_litellm.assert_called_once()
        self.assertEqual(mocked_create_runner.call_args.kwargs["profile"], "ephemeral")

    def test_plan_next_adk_uses_ephemeral_run_config(self) -> None:
        captured: dict[str, object] = {}

        class _FakeRunner:
            async def run_async(self, **kwargs):
                captured.update(kwargs)
                yield pytypes.SimpleNamespace(
                    content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(text="planner output")])
                )

        runner = GuiTaskRunner(
            planner_model="test-planner",
            planner_api_key="test-key",
            planner_runner=_FakeRunner(),
            action_executor=lambda **_: {"ok": True},
            runtime=_FakeRuntime(),
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "screen.png"
            path.write_bytes(b"fake-png")
            final = asyncio.run(
                runner._plan_next_adk_async(  # type: ignore[attr-defined]
                    "task",
                    "plan",
                    {},
                    [],
                    CapturedScreen(base64_png="screen", width=10, height=10, path=str(path)),
                )
            )

        self.assertEqual(final, "planner output")
        run_config = captured["run_config"]
        self.assertEqual(run_config.custom_metadata["profile"], "ephemeral")
        self.assertEqual(run_config.custom_metadata["request_kind"], "gui_planner")
        self.assertEqual(run_config.max_llm_calls, 8)
        self.assertFalse(run_config.model_dump(mode="python")["save_input_blobs_as_artifacts"])
        self.assertEqual(run_config.get_session_config.num_recent_events, 0)

    def test_task_runner_execute_then_reply(self) -> None:
        planned = [
            '{"thinking":"step1","action":{"type":"execute","params":{"action":"click login button"}}}',
            '{"thinking":"done","action":{"type":"reply","params":{"message":"login completed"}}}',
        ]
        actions: list[str] = []

        def _fake_action_executor(*, action: str, dry_run: bool = False) -> dict:
            actions.append(action)
            return {
                "ok": True,
                "screen_changed": True,
                "retries_used": 0,
                "raw_model_output": '{"action":"left_click","coordinate":[500,500]}',
                "tool_call": {"action": "left_click", "coordinate": [500, 500]},
                "screenshots": {"before_path": "/tmp/before.png", "after_path": "/tmp/after.png"},
            }

        runner = GuiTaskRunner(
            planner_model="test-planner",
            planner_api_key="test-key",
            planner_runner=object(),
            action_executor=_fake_action_executor,
            runtime=_FakeRuntime(),
        )
        with unittest.mock.patch.object(
            runner,
            "_plan_next_adk_async",
            new=unittest.mock.AsyncMock(side_effect=planned),
        ):
            result = runner.run("log in to website", max_steps=4)

        self.assertTrue(result["ok"])
        self.assertTrue(result["finished"])
        self.assertEqual(result["status_code"], "completed")
        self.assertEqual(result["last_error_type"], "none")
        self.assertEqual(result["saved_info_snapshot"], {})
        self.assertEqual(result["message"], "login completed")
        self.assertIn("plan=", result["final_summary"])
        self.assertIn("steps=1", result["final_summary"])
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["type"], "execute")
        self.assertEqual(result["steps"][0]["action"], "click login button")
        self.assertEqual(result["steps"][0]["planner_raw_model_output"], planned[0])
        self.assertEqual(result["steps"][0]["executor_raw_model_output"], '{"action":"left_click","coordinate":[500,500]}')
        self.assertEqual(result["steps"][0]["screenshots"]["before_path"], "/tmp/before.png")
        self.assertEqual(actions, ["click login button"])

    def test_task_runner_passes_cancel_token_to_action_executor(self) -> None:
        planned = [
            '{"thinking":"step1","action":{"type":"execute","params":{"action":"click login button"}}}',
            '{"thinking":"done","action":{"type":"reply","params":{"message":"login completed"}}}',
        ]
        observed: dict[str, object] = {}

        def _fake_action_executor(
            *,
            action: str,
            dry_run: bool = False,
            cancel_token: object | None = None,
        ) -> dict:
            observed["action"] = action
            observed["dry_run"] = dry_run
            observed["cancel_token"] = cancel_token
            return {"ok": True, "screen_changed": True, "retries_used": 0}

        runner = GuiTaskRunner(
            planner_model="test-planner",
            planner_api_key="test-key",
            planner_runner=object(),
            action_executor=_fake_action_executor,
            runtime=_FakeRuntime(),
        )
        cancel_token = SyncCancellationToken()
        with unittest.mock.patch.object(
            runner,
            "_plan_next_adk_async",
            new=unittest.mock.AsyncMock(side_effect=planned),
        ):
            result = runner.run("log in to website", max_steps=4, cancel_token=cancel_token)

        self.assertTrue(result["ok"])
        self.assertEqual(observed["action"], "click login button")
        self.assertIs(observed["cancel_token"], cancel_token)

    def test_task_runner_supports_save_info_and_modify_plan(self) -> None:
        planned = [
            '{"thinking":"remember user","action":{"type":"save_info","params":{"key":"username","value":"alice"}}}',
            '{"thinking":"refine plan","action":{"type":"modify_plan","params":{"new_plan":"1) open app 2) submit form"}}}',
            '{"thinking":"do it","action":{"type":"execute","params":{"action":"click submit"}}}',
            '{"thinking":"done","action":{"type":"reply","params":{"message":"submitted"}}}',
        ]
        actions: list[str] = []

        def _fake_action_executor(*, action: str, dry_run: bool = False) -> dict:
            actions.append(action)
            return {"ok": True, "screen_changed": True, "retries_used": 0}

        runner = GuiTaskRunner(
            planner_model="test-planner",
            planner_api_key="test-key",
            planner_runner=object(),
            action_executor=_fake_action_executor,
            runtime=_FakeRuntime(),
        )
        with unittest.mock.patch.object(
            runner,
            "_plan_next_adk_async",
            new=unittest.mock.AsyncMock(side_effect=planned),
        ):
            result = runner.run("submit the form", max_steps=6)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], "completed")
        self.assertEqual(result["saved_info"]["username"], "alice")
        self.assertEqual(result["saved_info_snapshot"]["username"], "alice")
        self.assertEqual(result["current_plan"], "1) open app 2) submit form")
        self.assertIn("saved_info=username=alice", result["final_summary"])
        self.assertIn("steps=3", result["final_summary"])
        self.assertEqual([step["type"] for step in result["steps"]], ["save_info", "modify_plan", "execute"])
        self.assertEqual(actions, ["click submit"])

    def test_task_runner_save_info_requires_key(self) -> None:
        planned = ['{"thinking":"bad save_info","action":{"type":"save_info","params":{"value":"alice"}}}']
        runner = GuiTaskRunner(
            planner_model="test-planner",
            planner_api_key="test-key",
            planner_runner=object(),
            action_executor=lambda **_: {"ok": True},
            runtime=_FakeRuntime(),
        )
        with unittest.mock.patch.object(
            runner,
            "_plan_next_adk_async",
            new=unittest.mock.AsyncMock(side_effect=planned),
        ):
            result = runner.run("submit the form", max_steps=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], "failed")
        self.assertEqual(result["last_error_type"], "missing_save_info_key")
        self.assertIn("missing params.key", result["error"])
        self.assertIn("steps=0", result["final_summary"])

    def test_task_runner_stops_on_no_progress(self) -> None:
        planned = [
            '{"thinking":"s1","action":{"type":"execute","params":{"action":"press Enter in address bar"}}}',
            '{"thinking":"s2","action":{"type":"execute","params":{"action":"press Enter in address bar"}}}',
        ]
        runner = GuiTaskRunner(
            planner_model="test-planner",
            planner_api_key="test-key",
            planner_runner=object(),
            action_executor=lambda **_: {"ok": True, "screen_changed": False, "retries_used": 0},
            runtime=_FakeRuntime(),
            max_no_progress_steps=2,
        )

        with unittest.mock.patch.object(
            runner,
            "_plan_next_adk_async",
            new=unittest.mock.AsyncMock(side_effect=planned),
        ):
            result = runner.run("search openppx", max_steps=5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], "no_progress")
        self.assertEqual(result["last_error_type"], "no_progress_stall")
        self.assertIn("no progress", result["error"])

    def test_task_runner_stops_on_repeated_action(self) -> None:
        planned = [
            '{"thinking":"s1","action":{"type":"execute","params":{"action":"click reload button"}}}',
            '{"thinking":"s2","action":{"type":"execute","params":{"action":"click reload button"}}}',
            '{"thinking":"s3","action":{"type":"execute","params":{"action":"click reload button"}}}',
        ]
        runner = GuiTaskRunner(
            planner_model="test-planner",
            planner_api_key="test-key",
            planner_runner=object(),
            action_executor=lambda **_: {"ok": True, "screen_changed": True, "retries_used": 0},
            runtime=_FakeRuntime(),
            max_repeat_actions=3,
        )

        with unittest.mock.patch.object(
            runner,
            "_plan_next_adk_async",
            new=unittest.mock.AsyncMock(side_effect=planned),
        ):
            result = runner.run("refresh page", max_steps=6)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], "no_progress")
        self.assertEqual(result["last_error_type"], "repeated_action_stall")
        self.assertIn("same action repeated", result["error"])

    def test_messages_include_concrete_action_constraints(self) -> None:
        runner = GuiTaskRunner(
            planner_model="test-planner",
            planner_api_key="test-key",
            planner_runner=object(),
            action_executor=lambda **_: {"ok": True},
            runtime=_FakeRuntime(),
        )
        messages = runner._messages(  # type: ignore[attr-defined]
            task="打开浏览器并搜索 openppx",
            current_plan="打开浏览器并搜索 openppx",
            saved_info={},
            history=[],
            screen=CapturedScreen(
                base64_png="screen",
                width=1920,
                height=1080,
                path="/tmp/x.png",
            ),
        )
        system_text = str(messages[0]["content"])
        self.assertIn("Execute params.action must be specific and observable", system_text)
        self.assertIn("Avoid vague actions like", system_text)

    def test_messages_include_correction_hint_when_unchanged(self) -> None:
        runner = GuiTaskRunner(
            planner_model="test-planner",
            planner_api_key="test-key",
            planner_runner=object(),
            action_executor=lambda **_: {"ok": True},
            runtime=_FakeRuntime(),
        )
        history = [
            {
                "step": 1,
                "type": "execute",
                "action": "search",
                "ok": True,
                "screen_changed": False,
                "retries_used": 0,
                "error": None,
            }
        ]
        messages = runner._messages(  # type: ignore[attr-defined]
            task="打开浏览器并搜索 openppx",
            current_plan="打开浏览器并搜索 openppx",
            saved_info={},
            history=history,
            screen=CapturedScreen(
                base64_png="screen",
                width=1920,
                height=1080,
                path="/tmp/x.png",
            ),
        )
        user_text = str(messages[1]["content"][0]["text"])
        self.assertIn("Correction hint", user_text)
        self.assertIn("did not change the screen", user_text)

    def test_execute_gui_task_uses_adk_only_runner(self) -> None:
        captured: dict[str, object] = {}

        class _FakeRunner:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

            def run(self, task: str, *, max_steps: int = 8, dry_run: bool = False) -> dict[str, object]:
                return {"ok": True, "task": task, "max_steps": max_steps, "dry_run": dry_run}

        with unittest.mock.patch("openppx.gui.task_runner.GuiTaskRunner", _FakeRunner):
            with unittest.mock.patch.dict(
                "os.environ",
                {
                    "OPENPPX_GUI_MODEL": "test-model",
                    "OPENPPX_GUI_GROUNDING_PROVIDER": "openai",
                    "OPENAI_API_KEY": "test-key",
                },
                clear=False,
            ):
                result = execute_gui_task(task="open browser", max_steps=3, dry_run=True)

        self.assertTrue(result["ok"])
        self.assertNotIn("use_adk_planner", captured)


if __name__ == "__main__":
    unittest.main()
