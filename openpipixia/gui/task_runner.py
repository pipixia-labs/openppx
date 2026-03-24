"""Multi-step GUI task runner built on top of computer_use."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from ..core.logging_utils import debug_logging_enabled, emit_debug
from ..core.provider import canonical_provider_name, provider_api_key_env
from ..runtime.adk_utils import extract_text, merge_text_stream
from .executor import (
    DEFAULT_GUI_BASE_URL_ENV,
    DEFAULT_GUI_GROUNDING_PROVIDER_ENV,
    DEFAULT_GUI_MODEL_ENV,
    CapturedScreen,
    PyAutoGuiRuntime,
    execute_gui_action,
)
from .prompts import load_planner_system_prompt


DEFAULT_GUI_PLANNER_MODEL_ENV = "OPENPIPIXIA_GUI_PLANNER_MODEL"
DEFAULT_GUI_PLANNER_PROVIDER_ENV = "OPENPIPIXIA_GUI_PLANNER_PROVIDER"
DEFAULT_GUI_PLANNER_BASE_URL_ENV = "OPENPIPIXIA_GUI_PLANNER_BASE_URL"
DEFAULT_GUI_TASK_MAX_STEPS_ENV = "OPENPIPIXIA_GUI_TASK_MAX_STEPS"
DEFAULT_GUI_TASK_PARSE_RETRIES_ENV = "OPENPIPIXIA_GUI_TASK_PARSE_RETRIES"
DEFAULT_GUI_TASK_MAX_NO_PROGRESS_STEPS_ENV = "OPENPIPIXIA_GUI_TASK_MAX_NO_PROGRESS_STEPS"
DEFAULT_GUI_TASK_MAX_REPEAT_ACTIONS_ENV = "OPENPIPIXIA_GUI_TASK_MAX_REPEAT_ACTIONS"


def _parse_action_json(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```json"):
        text = text.replace("```json", "", 1).strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return json.loads(text)


def _needs_correction_hint(history: list[dict[str, Any]]) -> bool:
    """Return True when latest execute step did not change the screen."""
    if not history:
        return False
    last = history[-1]
    return (
        str(last.get("type", "")).strip().lower() == "execute"
        and last.get("screen_changed") is False
        and bool(last.get("ok", False))
    )


def _run_coro_sync(coro: Any) -> Any:
    """Execute a coroutine from sync code, even if caller already has an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as exc:  # pragma: no cover - threading path
            result["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    if "error" in result:
        raise result["error"]
    return result.get("value")


def _debug(tag: str, payload: object, *, depth: int = 1) -> None:
    """Emit GUI task-runner debug log when debug mode is enabled."""
    if not debug_logging_enabled():
        return
    emit_debug(tag, payload, depth=depth + 1)


def _preview_text(text: str, *, max_chars: int = 800) -> str:
    """Return compact one-line preview for debug logging."""
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[:max_chars]}..."


class GuiTaskRunner:
    """Run a multi-step GUI task by iterating planner + computer_use execution."""

    def __init__(
        self,
        *,
        planner_model: str,
        planner_api_key: str,
        planner_provider: str = "",
        planner_base_url: str | None = None,
        action_executor: Callable[..., dict[str, Any]] | None = None,
        runtime: Any | None = None,
        planner_runner: Any | None = None,
        max_parse_retries: int = 1,
        max_no_progress_steps: int = 3,
        max_repeat_actions: int = 3,
    ) -> None:
        self._planner_model = planner_model
        self._planner_runner: Any = planner_runner or self._build_adk_planner_runner(
            planner_model=planner_model,
            planner_api_key=planner_api_key,
            planner_provider=planner_provider,
            planner_base_url=planner_base_url,
        )
        self._planner_user_id = "gui_planner"
        self._planner_session_id = "gui_planner:main"
        self._action_executor = action_executor or execute_gui_action
        self._runtime = runtime or PyAutoGuiRuntime()
        self._max_parse_retries = max(0, int(max_parse_retries))
        self._max_no_progress_steps = max(1, int(max_no_progress_steps))
        self._max_repeat_actions = max(1, int(max_repeat_actions))

    @staticmethod
    def _build_adk_planner_runner(
        *,
        planner_model: str,
        planner_api_key: str,
        planner_provider: str,
        planner_base_url: str | None,
    ) -> Any:
        """Create one ADK runner dedicated to GUI planning."""
        from google.adk.agents import LlmAgent
        from ..runtime.runner_factory import create_runner

        model: Any = planner_model
        if planner_provider != "google" and (planner_api_key or planner_base_url):
            from google.adk.models.lite_llm import LiteLlm

            kwargs: dict[str, Any] = {"drop_params": True}
            if planner_api_key:
                kwargs["api_key"] = planner_api_key
            if planner_base_url:
                kwargs["api_base"] = planner_base_url
            model = LiteLlm(model=planner_model, **kwargs)

        planner_agent = LlmAgent(
            name="openpipixia_gui_planner",
            model=model,
            instruction=load_planner_system_prompt(),
        )
        runner, _ = create_runner(agent=planner_agent, app_name="openpipixia_gui_planner")
        return runner

    @staticmethod
    def _render_history_text(history: list[dict[str, Any]]) -> str:
        """Render recent task history for planner prompt context."""
        if not history:
            return "No previous GUI steps."
        lines: list[str] = []
        for idx, item in enumerate(history[-8:], 1):
            lines.append(
                f"{idx}. type={item.get('type')} action={item.get('action')} changed={item.get('screen_changed')} "
                f"retries={item.get('retries_used')} ok={item.get('ok')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _render_saved_info_text(saved_info: dict[str, str]) -> str:
        """Render saved key-value context for planner prompt."""
        if not saved_info:
            return "No saved info."
        return "\n".join([f"- {k}: {v}" for k, v in saved_info.items()])

    @staticmethod
    def _render_correction_hint(history: list[dict[str, Any]]) -> str:
        """Return correction hint when latest execute step had no visible progress."""
        if not _needs_correction_hint(history):
            return ""
        return (
            "Correction hint:\n"
            "- The previous execute step did not change the screen.\n"
            "- First diagnose focus/state, then issue a more concrete action.\n"
            "- Do not repeat the same vague command.\n\n"
        )

    def _build_planner_user_text(
        self,
        task: str,
        current_plan: str,
        saved_info: dict[str, str],
        history: list[dict[str, Any]],
    ) -> str:
        """Build planner user prompt text shared by OpenAI/ADK paths."""
        history_text = self._render_history_text(history)
        saved_info_text = self._render_saved_info_text(saved_info)
        correction_hint = self._render_correction_hint(history)
        return (
            f"Task:\n{task}\n\n"
            f"Current plan:\n{current_plan}\n\n"
            f"Saved info:\n{saved_info_text}\n\n"
            f"Recent history:\n{history_text}\n\n"
            f"{correction_hint}"
            "Decide next action."
        )

    def _messages(
        self,
        task: str,
        current_plan: str,
        saved_info: dict[str, str],
        history: list[dict[str, Any]],
        screen: CapturedScreen,
    ) -> list[dict[str, Any]]:
        user_text = self._build_planner_user_text(task, current_plan, saved_info, history)
        return [
            {
                "role": "system",
                "content": load_planner_system_prompt(),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": user_text,
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{screen.base64_png}"},
                    },
                ],
            },
        ]

    async def _plan_next_adk_async(
        self,
        task: str,
        current_plan: str,
        saved_info: dict[str, str],
        history: list[dict[str, Any]],
        screen: CapturedScreen,
    ) -> str:
        """Run one planner request through ADK runner and return final text."""
        from google.genai import types

        prompt_text = self._build_planner_user_text(task, current_plan, saved_info, history)
        parts: list[Any] = [types.Part.from_text(text=prompt_text)]
        try:
            image_bytes = Path(screen.path).read_bytes()
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/png"))
        except Exception:
            _debug(
                "gui.task_runner.adk.image_read_fallback",
                {"screen_path": screen.path},
            )
        request = types.UserContent(parts=parts)

        final = ""
        async for event in self._planner_runner.run_async(
            user_id=self._planner_user_id,
            session_id=self._planner_session_id,
            new_message=request,
        ):
            text = extract_text(getattr(event, "content", None))
            final = merge_text_stream(final, text)
        return final

    def _plan_next(
        self,
        task: str,
        current_plan: str,
        saved_info: dict[str, str],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        parse_attempt = 0
        last_error = ""
        while parse_attempt <= self._max_parse_retries:
            screen = self._runtime.capture()
            raw = str(
                _run_coro_sync(
                    self._plan_next_adk_async(task, current_plan, saved_info, history, screen)
                )
                or ""
            )
            _debug(
                "gui.task_runner.parse_attempt",
                {
                    "attempt": parse_attempt + 1,
                    "mode": "adk",
                    "raw_preview": _preview_text(raw),
                },
            )
            try:
                parsed = _parse_action_json(raw)
                action = parsed.get("action")
                if not isinstance(action, dict):
                    raise ValueError("missing action object")
                parsed.setdefault("raw_model_output", raw)
                _debug(
                    "gui.task_runner.parse_success",
                    {
                        "attempt": parse_attempt + 1,
                        "action_type": str(action.get("type", "")).strip().lower(),
                    },
                )
                return parsed
            except Exception as exc:
                last_error = str(exc)
                _debug(
                    "gui.task_runner.parse_failed",
                    {
                        "attempt": parse_attempt + 1,
                        "error": last_error,
                        "raw_preview": _preview_text(raw),
                    },
                )
                parse_attempt += 1
                if parse_attempt > self._max_parse_retries:
                    raise ValueError(
                        f"failed to parse task planner output after {self._max_parse_retries + 1} attempts: {last_error}"
                    ) from exc
        raise ValueError("planner parsing fallback reached unexpectedly")

    def run(self, task: str, *, max_steps: int = 8, dry_run: bool = False) -> dict[str, Any]:
        """Run task loop until reply or max steps."""
        _debug(
            "gui.task_runner.run.start",
            {
                "task": task,
                "max_steps": max_steps,
                "dry_run": dry_run,
                "mode": "adk",
                "max_parse_retries": self._max_parse_retries,
                "max_no_progress_steps": self._max_no_progress_steps,
                "max_repeat_actions": self._max_repeat_actions,
            },
        )
        history: list[dict[str, Any]] = []
        current_plan = task
        saved_info: dict[str, str] = {}

        def _final_summary() -> str:
            if saved_info:
                saved_info_text = ", ".join([f"{k}={v}" for k, v in saved_info.items()])
            else:
                saved_info_text = "none"
            return f"plan={current_plan}; saved_info={saved_info_text}; steps={len(history)}"

        def _result(
            *,
            ok: bool,
            finished: bool,
            status_code: str,
            message: str | None = None,
            error: str | None = None,
            last_error_type: str | None = None,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "ok": ok,
                "task": task,
                "steps": history,
                "step_count": len(history),
                "current_plan": current_plan,
                "saved_info": saved_info,
                "saved_info_snapshot": dict(saved_info),
                "final_summary": _final_summary(),
                "finished": finished,
                "status_code": status_code,
                "last_error_type": last_error_type or "none",
            }
            if message is not None:
                payload["message"] = message
            if error is not None:
                payload["error"] = error
            return payload

        for step in range(1, max_steps + 1):
            planned = self._plan_next(task, current_plan, saved_info, history)
            action = planned.get("action", {})
            action_type = str(action.get("type", "")).strip().lower()
            params = action.get("params", {}) if isinstance(action.get("params"), dict) else {}
            _debug(
                "gui.task_runner.step",
                {
                    "step": step,
                    "action_type": action_type,
                    "thinking_preview": _preview_text(str(planned.get("thinking", "")), max_chars=200),
                },
            )

            if action_type == "reply":
                message = str(params.get("message", "Task finished")).strip() or "Task finished"
                return _result(
                    ok=True,
                    finished=True,
                    status_code="completed",
                    message=message,
                )

            if action_type == "save_info":
                key = str(params.get("key", "")).strip()
                value = str(params.get("value", "")).strip()
                if not key:
                    return _result(
                        ok=False,
                        finished=False,
                        status_code="failed",
                        error="planner save_info action missing params.key",
                        last_error_type="missing_save_info_key",
                    )
                saved_info[key] = value
                history.append(
                    {
                        "step": step,
                        "type": "save_info",
                        "thinking": planned.get("thinking", ""),
                        "planner_raw_model_output": planned.get("raw_model_output"),
                        "action": f"save_info:{key}",
                        "ok": True,
                        "screen_changed": None,
                        "retries_used": 0,
                        "error": None,
                    }
                )
                continue

            if action_type == "modify_plan":
                new_plan = str(params.get("new_plan", "")).strip()
                if not new_plan:
                    return _result(
                        ok=False,
                        finished=False,
                        status_code="failed",
                        error="planner modify_plan action missing params.new_plan",
                        last_error_type="missing_modify_plan_value",
                    )
                current_plan = new_plan
                history.append(
                    {
                        "step": step,
                        "type": "modify_plan",
                        "thinking": planned.get("thinking", ""),
                        "planner_raw_model_output": planned.get("raw_model_output"),
                        "action": "modify_plan",
                        "ok": True,
                        "screen_changed": None,
                        "retries_used": 0,
                        "error": None,
                    }
                )
                continue

            if action_type != "execute":
                return _result(
                    ok=False,
                    finished=False,
                    status_code="failed",
                    error=f"unsupported planner action type: {action_type}",
                    last_error_type="unsupported_action_type",
                )

            action_text = str(params.get("action", "")).strip()
            if not action_text:
                return _result(
                    ok=False,
                    finished=False,
                    status_code="failed",
                    error="planner execute action missing params.action",
                    last_error_type="missing_execute_action",
                )

            result = self._action_executor(action=action_text, dry_run=dry_run)
            step_record = {
                "step": step,
                "type": "execute",
                "thinking": planned.get("thinking", ""),
                "planner_raw_model_output": planned.get("raw_model_output"),
                "action": action_text,
                "ok": bool(result.get("ok", False)),
                "screen_changed": result.get("screen_changed"),
                "retries_used": result.get("retries_used"),
                "error": result.get("error"),
                "executor_raw_model_output": result.get("raw_model_output"),
                "executor_tool_call": result.get("tool_call"),
                "screenshots": result.get("screenshots"),
            }
            history.append(step_record)
            _debug(
                "gui.task_runner.execute_result",
                {
                    "step": step,
                    "action": action_text,
                    "ok": step_record["ok"],
                    "screen_changed": step_record["screen_changed"],
                    "retries_used": step_record["retries_used"],
                },
            )
            if not step_record["ok"]:
                return _result(
                       ok=False,
                    finished=False,
                    status_code="failed",
                    error=f"computer_use failed at step {step}: {step_record.get('error')}",
                    last_error_type="executor_error",
                )

            # Stall guard: stop early if progress is repeatedly absent.
            no_progress_count = 0
            for item in reversed(history):
                if item.get("type") != "execute":
                    break
                if item.get("screen_changed") is False:
                    no_progress_count += 1
                else:
                    break
            if no_progress_count >= self._max_no_progress_steps:
                return _result(
                    ok=False,
                    finished=False,
                    status_code="no_progress",
                    error=(
                        f"no progress for {no_progress_count} consecutive execute steps "
                        f"(threshold={self._max_no_progress_steps})"
                    ),
                    last_error_type="no_progress_stall",
                )

            repeated_action_count = 0
            for item in reversed(history):
                if item.get("type") != "execute":
                    break
                if str(item.get("action", "")).strip() == action_text:
                    repeated_action_count += 1
                else:
                    break
            if repeated_action_count >= self._max_repeat_actions:
                return _result(
                    ok=False,
                    finished=False,
                    status_code="no_progress",
                    error=(
                        f"same action repeated {repeated_action_count} times "
                        f"(threshold={self._max_repeat_actions}): {action_text}"
                    ),
                    last_error_type="repeated_action_stall",
                )

        return _result(
            ok=False,
            finished=False,
            status_code="max_steps",
            error=f"max steps reached ({max_steps})",
            last_error_type="max_steps_reached",
        )


def execute_gui_task(
    *,
    task: str,
    max_steps: int | None = None,
    dry_run: bool = False,
    planner_model: str | None = None,
    planner_api_key: str | None = None,
    planner_base_url: str | None = None,
) -> dict[str, Any]:
    """Run a multi-step GUI task using environment-resolved planner settings."""
    planner_provider = canonical_provider_name(
        os.getenv(DEFAULT_GUI_PLANNER_PROVIDER_ENV, "") or os.getenv(DEFAULT_GUI_GROUNDING_PROVIDER_ENV, "")
    )
    planner_api_key_env = provider_api_key_env(planner_provider) if planner_provider else None
    provider_api_key = os.getenv(planner_api_key_env, "").strip() if planner_api_key_env else ""
    resolved_planner_model = (
        planner_model
        or os.getenv(DEFAULT_GUI_PLANNER_MODEL_ENV, "")
        or os.getenv(DEFAULT_GUI_MODEL_ENV, "")
    ).strip()
    resolved_planner_api_key = (
        planner_api_key
        or provider_api_key
    ).strip()
    resolved_planner_base_url = (
        planner_base_url
        or os.getenv(DEFAULT_GUI_PLANNER_BASE_URL_ENV, "")
        or os.getenv(DEFAULT_GUI_BASE_URL_ENV, "")
    ).strip() or None
    resolved_max_steps = max_steps
    if resolved_max_steps is None:
        raw_steps = os.getenv(DEFAULT_GUI_TASK_MAX_STEPS_ENV, "").strip()
        try:
            resolved_max_steps = int(raw_steps) if raw_steps else 8
        except ValueError:
            resolved_max_steps = 8
    resolved_max_steps = max(1, int(resolved_max_steps))
    raw_parse_retries = os.getenv(DEFAULT_GUI_TASK_PARSE_RETRIES_ENV, "").strip()
    try:
        max_parse_retries = max(0, int(raw_parse_retries)) if raw_parse_retries else 1
    except ValueError:
        max_parse_retries = 1
    raw_no_progress_steps = os.getenv(DEFAULT_GUI_TASK_MAX_NO_PROGRESS_STEPS_ENV, "").strip()
    try:
        max_no_progress_steps = max(1, int(raw_no_progress_steps)) if raw_no_progress_steps else 3
    except ValueError:
        max_no_progress_steps = 3
    raw_repeat_actions = os.getenv(DEFAULT_GUI_TASK_MAX_REPEAT_ACTIONS_ENV, "").strip()
    try:
        max_repeat_actions = max(1, int(raw_repeat_actions)) if raw_repeat_actions else 3
    except ValueError:
        max_repeat_actions = 3

    if not resolved_planner_model:
        raise ValueError(
            f"Missing GUI planner model. Set {DEFAULT_GUI_PLANNER_MODEL_ENV} or {DEFAULT_GUI_MODEL_ENV}."
        )

    runner = GuiTaskRunner(
        planner_model=resolved_planner_model,
        planner_api_key=resolved_planner_api_key,
        planner_provider=planner_provider,
        planner_base_url=resolved_planner_base_url,
        max_parse_retries=max_parse_retries,
        max_no_progress_steps=max_no_progress_steps,
        max_repeat_actions=max_repeat_actions,
    )
    return runner.run(task, max_steps=resolved_max_steps, dry_run=dry_run)


__all__ = ["GuiTaskRunner", "execute_gui_task"]
