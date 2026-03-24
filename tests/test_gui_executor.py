"""Tests for GUI grounding executor."""

from __future__ import annotations

import unittest
import unittest.mock
from pathlib import Path

from openpipixia.gui.executor import CapturedScreen, GroundingExecutor, PyAutoGuiRuntime, execute_gui_action


class _FakeRuntime:
    def __init__(self, captures: list[str] | None = None) -> None:
        self.calls: list[dict] = []
        self._captures = captures[:] if captures else []
        self._capture_index = 0

    def capture(self) -> CapturedScreen:
        index = self._capture_index
        self._capture_index += 1
        base64_png = self._captures[index] if index < len(self._captures) else f"ZmFrZS0{index}"
        return CapturedScreen(
            base64_png=base64_png,
            width=1920,
            height=1080,
            path=f"/tmp/fake-{index}.png",
        )

    def perform(self, arguments: dict) -> None:
        self.calls.append(arguments)


class GuiExecutorTests(unittest.TestCase):
    def test_runtime_capture_falls_back_to_image_grab_when_pyautogui_unavailable(self) -> None:
        class _FakeShot:
            width = 1920
            height = 1080

            def save(self, path):
                Path(path).write_bytes(b"fake-png")

        class _FakeImageGrab:
            @staticmethod
            def grab():
                return _FakeShot()

        with unittest.mock.patch("openpipixia.gui.executor._load_pyautogui", return_value=None):
            with unittest.mock.patch("openpipixia.gui.executor._load_image_grab", return_value=_FakeImageGrab()):
                runtime = PyAutoGuiRuntime()
                captured = runtime.capture()
        self.assertEqual(captured.width, 1920)
        self.assertEqual(captured.height, 1080)
        self.assertTrue(captured.base64_png)

    def test_runtime_perform_lazy_loads_pyautogui(self) -> None:
        class _FakeAutoGui:
            class _Size:
                width = 1920
                height = 1080

            def size(self):
                return self._Size()

            def click(self, x, y):
                self.clicked = (x, y)

        fake_pyautogui = _FakeAutoGui()
        with unittest.mock.patch("openpipixia.gui.executor._load_pyautogui", return_value=fake_pyautogui):
            runtime = PyAutoGuiRuntime(pyperclip_module=object())
            runtime.perform({"action": "left_click", "coordinate": [10, 10]})

        self.assertEqual(fake_pyautogui.clicked, (19.2, 10.8))

    def test_build_runner_uses_native_google_model_without_litellm(self) -> None:
        with unittest.mock.patch("google.adk.models.lite_llm.LiteLlm") as mocked_litellm:
            with unittest.mock.patch("google.adk.agents.LlmAgent") as mocked_agent:
                with unittest.mock.patch(
                    "openpipixia.runtime.runner_factory.create_runner",
                    return_value=(object(), None),
                ):
                    GroundingExecutor._build_adk_grounding_runner(
                        model="gemini-3-flash-preview",
                        api_key="google-key",
                        provider="google",
                        base_url=None,
                    )

        mocked_litellm.assert_not_called()
        self.assertEqual(mocked_agent.call_args.kwargs["model"], "gemini-3-flash-preview")

    def test_build_runner_uses_litellm_for_non_google_provider(self) -> None:
        with unittest.mock.patch("google.adk.models.lite_llm.LiteLlm") as mocked_litellm:
            mocked_litellm.return_value = object()
            with unittest.mock.patch("google.adk.agents.LlmAgent"):
                with unittest.mock.patch(
                    "openpipixia.runtime.runner_factory.create_runner",
                    return_value=(object(), None),
                ):
                    GroundingExecutor._build_adk_grounding_runner(
                        model="openai/gpt-4.1-mini",
                        api_key="openai-key",
                        provider="openai",
                        base_url=None,
                    )

        mocked_litellm.assert_called_once()

    def test_executor_runs_with_tool_call_block(self) -> None:
        runtime = _FakeRuntime()
        executor = GroundingExecutor(
            model="test-model",
            api_key="test-key",
            runtime=runtime,
            grounding_runner=object(),
        )
        with unittest.mock.patch.object(
            executor,
            "_ground_with_adk",
            new=unittest.mock.AsyncMock(
                return_value='<tool_call>{"name":"computer_use","arguments":{"action":"left_click","coordinate":[500,500]}}</tool_call>'
            ),
        ):
            result = executor.run("click center")

        self.assertTrue(result["ok"])
        self.assertEqual(result["arguments"]["action"], "left_click")
        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(runtime.calls[0]["coordinate"], [500, 500])

    def test_executor_respects_dry_run(self) -> None:
        runtime = _FakeRuntime()
        executor = GroundingExecutor(
            model="test-model",
            api_key="test-key",
            runtime=runtime,
            grounding_runner=object(),
        )

        with unittest.mock.patch.object(
            executor,
            "_ground_with_adk",
            new=unittest.mock.AsyncMock(return_value='{"action":"wait","time":1}'),
        ):
            result = executor.run("wait", dry_run=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(runtime.calls, [])

    def test_runtime_blocks_dangerous_key_chord(self) -> None:
        class _FakeAutoGui:
            class _Size:
                width = 1920
                height = 1080

            def size(self):
                return self._Size()

        runtime = PyAutoGuiRuntime(
            pyautogui_module=_FakeAutoGui(),
            pyperclip_module=object(),
            allow_dangerous_keys=False,
        )

        with self.assertRaises(ValueError):
            runtime.perform({"action": "key", "keys": ["command", "q"]})

    def test_runtime_caps_wait_seconds(self) -> None:
        class _FakeAutoGui:
            class _Size:
                width = 1920
                height = 1080

            def size(self):
                return self._Size()

        runtime = PyAutoGuiRuntime(
            pyautogui_module=_FakeAutoGui(),
            pyperclip_module=object(),
            max_wait_seconds=0.1,
        )

        with unittest.mock.patch("openpipixia.gui.executor.time.sleep") as mocked_sleep:
            runtime.perform({"action": "wait", "time": 8})
        mocked_sleep.assert_called_once_with(0.1)

    def test_runtime_blocks_action_by_blocklist(self) -> None:
        class _FakeAutoGui:
            class _Size:
                width = 1920
                height = 1080

            def size(self):
                return self._Size()

        runtime = PyAutoGuiRuntime(
            pyautogui_module=_FakeAutoGui(),
            pyperclip_module=object(),
            blocked_actions={"scroll"},
        )

        with self.assertRaises(ValueError):
            runtime.perform({"action": "scroll", "pixels": -100})

    def test_runtime_blocks_action_not_in_allowlist(self) -> None:
        class _FakeAutoGui:
            class _Size:
                width = 1920
                height = 1080

            def size(self):
                return self._Size()

        runtime = PyAutoGuiRuntime(
            pyautogui_module=_FakeAutoGui(),
            pyperclip_module=object(),
            allowed_actions={"wait"},
        )

        with self.assertRaises(ValueError):
            runtime.perform({"action": "left_click", "coordinate": [10, 10]})

    def test_executor_retries_parse_then_succeeds(self) -> None:
        runtime = _FakeRuntime()
        executor = GroundingExecutor(
            model="test-model",
            api_key="test-key",
            runtime=runtime,
            grounding_runner=object(),
            max_parse_retries=1,
        )

        with unittest.mock.patch.object(
            executor,
            "_ground_with_adk",
            new=unittest.mock.AsyncMock(
                side_effect=["not-json", '{"name":"computer_use","arguments":{"action":"wait","time":1}}']
            ),
        ):
            result = executor.run("wait")

        self.assertTrue(result["ok"])
        self.assertEqual(result["arguments"]["action"], "wait")
        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(result["retries_used"], 0)

    def test_executor_parse_retry_exhausted(self) -> None:
        runtime = _FakeRuntime()
        executor = GroundingExecutor(
            model="test-model",
            api_key="test-key",
            runtime=runtime,
            grounding_runner=object(),
            max_parse_retries=1,
        )

        with unittest.mock.patch.object(
            executor,
            "_ground_with_adk",
            new=unittest.mock.AsyncMock(side_effect=["bad-output", "still-bad"]),
        ):
            with self.assertRaises(ValueError):
                executor.run("click")

    def test_executor_retries_when_screen_unchanged(self) -> None:
        runtime = _FakeRuntime(captures=["same", "same", "before-2", "after-2"])
        executor = GroundingExecutor(
            model="test-model",
            api_key="test-key",
            runtime=runtime,
            grounding_runner=object(),
            max_action_retries=1,
            verify_screen_change=True,
        )

        with unittest.mock.patch.object(
            executor,
            "_ground_with_adk",
            new=unittest.mock.AsyncMock(
                side_effect=[
                    '{"name":"computer_use","arguments":{"action":"left_click","coordinate":[500,500]}}',
                    '{"name":"computer_use","arguments":{"action":"left_click","coordinate":[500,500]}}',
                ]
            ),
        ):
            result = executor.run("click once")

        self.assertTrue(result["ok"])
        self.assertTrue(result["screen_changed"])
        self.assertEqual(result["retries_used"], 1)
        self.assertEqual(len(runtime.calls), 2)

    def test_execute_gui_action_keeps_adk_only_executor(self) -> None:
        captured: dict[str, object] = {}

        class _FakeExecutor:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

            def run(self, action: str, *, dry_run: bool = False) -> dict[str, object]:
                return {"ok": True, "action": action, "dry_run": dry_run}

        with unittest.mock.patch("openpipixia.gui.executor.GroundingExecutor", _FakeExecutor):
            with unittest.mock.patch.dict(
                "os.environ",
                {
                    "OPENPIPIXIA_GUI_MODEL": "test-model",
                    "OPENPIPIXIA_GUI_GROUNDING_PROVIDER": "openai",
                    "OPENAI_API_KEY": "test-key",
                },
                clear=False,
            ):
                result = execute_gui_action(action="click button", dry_run=True)

        self.assertTrue(result["ok"])
        self.assertNotIn("use_adk_grounding", captured)


if __name__ == "__main__":
    unittest.main()
