"""GUI grounding executor for single-step desktop actions."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..core.logging_utils import debug_logging_enabled, emit_debug
from ..core.provider import canonical_provider_name, provider_api_key_env
from ..runtime.adk_utils import extract_text, merge_text_stream
from .prompts import load_executor_system_prompt

DEFAULT_GUI_MODEL_ENV = "OPENPIPIXIA_GUI_MODEL"
DEFAULT_GUI_GROUNDING_PROVIDER_ENV = "OPENPIPIXIA_GUI_GROUNDING_PROVIDER"
DEFAULT_GUI_BASE_URL_ENV = "OPENPIPIXIA_GUI_BASE_URL"
DEFAULT_GUI_ALLOW_DANGEROUS_KEYS_ENV = "OPENPIPIXIA_GUI_ALLOW_DANGEROUS_KEYS"
DEFAULT_GUI_MAX_WAIT_SECONDS_ENV = "OPENPIPIXIA_GUI_MAX_WAIT_SECONDS"
DEFAULT_GUI_MAX_PARSE_RETRIES_ENV = "OPENPIPIXIA_GUI_MAX_PARSE_RETRIES"
DEFAULT_GUI_VERIFY_SCREEN_CHANGE_ENV = "OPENPIPIXIA_GUI_VERIFY_SCREEN_CHANGE"
DEFAULT_GUI_MAX_ACTION_RETRIES_ENV = "OPENPIPIXIA_GUI_MAX_ACTION_RETRIES"
DEFAULT_GUI_ALLOWED_ACTIONS_ENV = "OPENPIPIXIA_GUI_ALLOWED_ACTIONS"
DEFAULT_GUI_BLOCKED_ACTIONS_ENV = "OPENPIPIXIA_GUI_BLOCKED_ACTIONS"

_DANGEROUS_KEY_CHORDS: tuple[frozenset[str], ...] = (
    frozenset({"command", "q"}),
    frozenset({"alt", "f4"}),
    frozenset({"ctrl", "alt", "delete"}),
    frozenset({"command", "option", "esc"}),
)

_RETRYABLE_SCREEN_CHANGE_ACTIONS: frozenset[str] = frozenset(
    {
        "mouse_move",
        "left_click",
        "double_click",
        "right_click",
        "left_click_drag",
        "scroll",
        "type",
    }
)


@dataclass(frozen=True)
class CapturedScreen:
    """One captured screenshot payload for grounding."""

    base64_png: str
    width: int
    height: int
    path: str


class GuiRuntime(Protocol):
    """Runtime contract used by grounding executor."""

    def capture(self) -> CapturedScreen:
        """Capture the current screen and return encoded payload."""

    def perform(self, arguments: dict[str, Any]) -> None:
        """Perform one parsed GUI action."""


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
    """Emit GUI executor debug log when debug mode is enabled."""
    if not debug_logging_enabled():
        return
    emit_debug(tag, payload, depth=depth + 1)


def _preview_text(text: str, *, max_chars: int = 800) -> str:
    """Return compact one-line preview for debug logging."""
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[:max_chars]}..."


def _load_pyautogui() -> Any:
    try:
        import pyautogui  # type: ignore
    except Exception:  # pragma: no cover - runtime dependent
        return None
    return pyautogui


def _load_pyperclip() -> Any:
    try:
        import pyperclip  # type: ignore
    except Exception:  # pragma: no cover - runtime dependent
        return None
    return pyperclip


def _load_image_grab() -> Any:
    try:
        from PIL import ImageGrab  # type: ignore
    except Exception:  # pragma: no cover - runtime dependent
        return None
    return ImageGrab


class PyAutoGuiRuntime:
    """GUI runtime backed by pyautogui/pyperclip."""

    def __init__(
        self,
        *,
        screenshot_dir: str | None = None,
        allow_dangerous_keys: bool = False,
        max_wait_seconds: float = 5.0,
        allowed_actions: set[str] | None = None,
        blocked_actions: set[str] | None = None,
        pyautogui_module: Any | None = None,
        pyperclip_module: Any | None = None,
    ) -> None:
        self._pyautogui = pyautogui_module or _load_pyautogui()
        self._pyperclip = pyperclip_module or _load_pyperclip()
        self._image_grab = _load_image_grab()
        self._allow_dangerous_keys = bool(allow_dangerous_keys)
        self._max_wait_seconds = float(max_wait_seconds)
        self._allowed_actions = set([a.strip().lower() for a in (allowed_actions or set()) if a.strip()])
        self._blocked_actions = set([a.strip().lower() for a in (blocked_actions or set()) if a.strip()])
        self._screenshot_dir = Path(
            screenshot_dir or os.path.join(tempfile.gettempdir(), "openpipixia_gui")
        )
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)

    def capture(self) -> CapturedScreen:
        """Capture a screenshot and encode to base64 PNG."""
        if self._pyautogui is not None:
            shot = self._pyautogui.screenshot()
        elif self._image_grab is not None:
            shot = self._image_grab.grab()
        else:  # pragma: no cover - runtime dependent
            raise RuntimeError(
                "No screenshot backend available. Install pyautogui or Pillow ImageGrab support."
            )
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        path = self._screenshot_dir / f"screenshot-{timestamp}.png"
        shot.save(path)
        image_bytes = path.read_bytes()
        return CapturedScreen(
            base64_png=base64.b64encode(image_bytes).decode("utf-8"),
            width=int(getattr(shot, "width", 0) or 0),
            height=int(getattr(shot, "height", 0) or 0),
            path=str(path),
        )

    @staticmethod
    def _to_absolute_coordinate(arguments: dict[str, Any], width: int, height: int) -> tuple[float, float]:
        raw = arguments.get("coordinate", [0, 0])
        if not isinstance(raw, list) or len(raw) < 2:
            return 0.0, 0.0
        x = float(raw[0])
        y = float(raw[1])
        if x <= 1000 and y <= 1000:
            return (x / 1000.0) * width, (y / 1000.0) * height
        return x, y

    def _validate_key_action(self, keys: list[str]) -> None:
        normalized = [key.strip().lower() for key in keys if key and key.strip()]
        if not normalized:
            raise ValueError("key action requires non-empty keys")
        if self._allow_dangerous_keys:
            return
        pressed = frozenset(normalized)
        for blocked in _DANGEROUS_KEY_CHORDS:
            if blocked.issubset(pressed):
                raise ValueError(f"blocked dangerous key chord: {'+'.join(sorted(blocked))}")

    def _validate_action_policy(self, action: str) -> None:
        if action in self._blocked_actions:
            raise ValueError(f"action blocked by policy: {action}")
        if self._allowed_actions and action not in self._allowed_actions:
            raise ValueError(f"action not in allowlist: {action}")

    def perform(self, arguments: dict[str, Any]) -> None:
        """Execute one GUI action."""
        if self._pyautogui is None:  # pragma: no cover - runtime dependent
            raise RuntimeError(
                "pyautogui is required for non-dry-run GUI actions. Install pyautogui to execute actions."
            )
        action = str(arguments.get("action", "")).strip().lower()
        self._validate_action_policy(action)
        screen = self._pyautogui.size()
        width = int(getattr(screen, "width", 0) or 0)
        height = int(getattr(screen, "height", 0) or 0)
        x, y = self._to_absolute_coordinate(arguments, width, height)

        if action == "key":
            keys = [str(k) for k in (arguments.get("keys") or [])]
            self._validate_key_action(keys)
            if len(keys) == 1:
                self._pyautogui.press(keys[0])
                return
            for key in keys[:-1]:
                self._pyautogui.keyDown(key)
            if keys:
                self._pyautogui.press(keys[-1])
            for key in reversed(keys[:-1]):
                self._pyautogui.keyUp(key)
            return

        if action == "type":
            text = str(arguments.get("text", ""))
            self._pyperclip.copy(text)
            self._pyautogui.hotkey("command" if os.name != "nt" else "ctrl", "v")
            return

        if action == "mouse_move":
            self._pyautogui.moveTo(x, y)
            return

        if action == "left_click":
            self._pyautogui.click(x, y)
            return

        if action == "double_click":
            self._pyautogui.doubleClick(x, y)
            return

        if action == "right_click":
            self._pyautogui.rightClick(x, y)
            return

        if action == "left_click_drag":
            self._pyautogui.dragTo(x, y, duration=0.5)
            return

        if action == "scroll":
            self._pyautogui.scroll(int(arguments.get("pixels", -500)))
            return

        if action == "wait":
            requested = float(arguments.get("time", 1.0))
            wait_seconds = max(0.0, min(requested, self._max_wait_seconds))
            time.sleep(wait_seconds)
            return

        raise ValueError(f"Unsupported GUI action: {action}")


def _tool_call_payload(content: str) -> dict[str, Any]:
    """Extract model tool_call payload from response content."""
    text = (content or "").strip()
    if not text:
        raise ValueError("empty model output")

    if "<tool_call>" in text and "</tool_call>" in text:
        body = text.split("<tool_call>", 1)[1].split("</tool_call>", 1)[0].strip()
        return json.loads(body)
    return json.loads(text)


def _normalize_tool_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize tool payload to arguments dict."""
    if "arguments" in payload and isinstance(payload["arguments"], dict):
        return payload["arguments"]
    if "action" in payload:
        return payload
    raise ValueError("model output missing action arguments")


class GroundingExecutor:
    """Single-step GUI grounding executor."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        provider: str = "",
        base_url: str | None = None,
        runtime: GuiRuntime | None = None,
        grounding_runner: Any | None = None,
        max_parse_retries: int = 1,
        verify_screen_change: bool = True,
        max_action_retries: int = 1,
    ) -> None:
        self._model = model
        self._runtime = runtime or PyAutoGuiRuntime()
        self._grounding_runner: Any = grounding_runner or self._build_adk_grounding_runner(
            model=model,
            api_key=api_key,
            provider=provider,
            base_url=base_url,
        )
        self._grounding_user_id = "gui_grounding"
        self._grounding_session_id = "gui_grounding:main"
        self._max_parse_retries = max(0, int(max_parse_retries))
        self._verify_screen_change = bool(verify_screen_change)
        self._max_action_retries = max(0, int(max_action_retries))

    @staticmethod
    def _build_adk_grounding_runner(
        *,
        model: str,
        api_key: str,
        provider: str,
        base_url: str | None,
    ) -> Any:
        """Create one ADK runner dedicated to single-step GUI grounding."""
        from google.adk.agents import LlmAgent
        from ..runtime.runner_factory import create_runner

        adk_model: Any = model
        if provider != "google" and (api_key or base_url):
            from google.adk.models.lite_llm import LiteLlm

            kwargs: dict[str, Any] = {"drop_params": True}
            if api_key:
                kwargs["api_key"] = api_key
            if base_url:
                kwargs["api_base"] = base_url
            adk_model = LiteLlm(model=model, **kwargs)

        agent = LlmAgent(
            name="openpipixia_gui_grounding",
            model=adk_model,
            instruction=load_executor_system_prompt(),
        )
        runner, _ = create_runner(agent=agent, app_name="openpipixia_gui_grounding")
        return runner

    async def _ground_with_adk(self, before: CapturedScreen, action: str) -> str:
        """Run one grounding request through ADK runner and return final text."""
        from google.genai import types

        parts: list[Any] = [types.Part.from_text(text=action)]
        try:
            image_bytes = Path(before.path).read_bytes()
            parts.insert(0, types.Part.from_bytes(data=image_bytes, mime_type="image/png"))
        except Exception:
            _debug(
                "gui.executor.adk.image_read_fallback",
                {"before_path": before.path},
            )
            pass
        request = types.UserContent(parts=parts)

        final = ""
        assert self._grounding_runner is not None
        async for event in self._grounding_runner.run_async(
            user_id=self._grounding_user_id,
            session_id=self._grounding_session_id,
            new_message=request,
        ):
            text = extract_text(getattr(event, "content", None))
            final = merge_text_stream(final, text)
        return final

    def run(self, action: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Execute one GUI action request end-to-end."""
        _debug(
            "gui.executor.run.start",
            {
                "action": action,
                "dry_run": dry_run,
                "model": self._model,
                "mode": "adk",
                "max_parse_retries": self._max_parse_retries,
                "max_action_retries": self._max_action_retries,
            },
        )
        action_retry_count = 0
        while True:
            before = self._runtime.capture()
            parse_attempt = 0
            last_error = ""
            raw_output = ""
            tool_payload: dict[str, Any] | None = None
            arguments: dict[str, Any] | None = None

            while parse_attempt <= self._max_parse_retries:
                raw_output = str(_run_coro_sync(self._ground_with_adk(before, action)) or "")
                _debug(
                    "gui.executor.parse_attempt",
                    {
                        "attempt": parse_attempt + 1,
                        "raw_preview": _preview_text(raw_output),
                    },
                )
                try:
                    tool_payload = _tool_call_payload(raw_output)
                    arguments = _normalize_tool_arguments(tool_payload)
                    _debug(
                        "gui.executor.parse_success",
                        {
                            "attempt": parse_attempt + 1,
                            "action": str(arguments.get("action", "")).strip().lower(),
                        },
                    )
                    break
                except Exception as exc:
                    last_error = str(exc)
                    _debug(
                        "gui.executor.parse_failed",
                        {
                            "attempt": parse_attempt + 1,
                            "error": last_error,
                            "raw_preview": _preview_text(raw_output),
                        },
                    )
                    parse_attempt += 1
                    if parse_attempt > self._max_parse_retries:
                        raise ValueError(
                            f"failed to parse grounding output after {self._max_parse_retries + 1} attempts: {last_error}"
                        ) from exc

            if tool_payload is None or arguments is None:
                raise ValueError("grounding parser did not produce action arguments")
            if not dry_run:
                self._runtime.perform(arguments)
            after = self._runtime.capture()
            screen_changed = before.base64_png != after.base64_png
            action_name = str(arguments.get("action", "")).strip().lower()

            should_retry = (
                not dry_run
                and self._verify_screen_change
                and not screen_changed
                and action_name in _RETRYABLE_SCREEN_CHANGE_ACTIONS
                and action_retry_count < self._max_action_retries
            )
            if should_retry:
                action_retry_count += 1
                _debug(
                    "gui.executor.action_retry",
                    {
                        "retry_count": action_retry_count,
                        "action_name": action_name,
                        "screen_changed": screen_changed,
                    },
                )
                continue

            result = {
                "ok": True,
                "action": action,
                "tool_call": tool_payload,
                "arguments": arguments,
                "dry_run": dry_run,
                "screen_changed": screen_changed,
                "retries_used": action_retry_count,
                "screenshots": {
                    "before_path": before.path,
                    "after_path": after.path,
                    "before_size": [before.width, before.height],
                    "after_size": [after.width, after.height],
                },
                "raw_model_output": raw_output,
            }
            _debug(
                "gui.executor.run.result",
                {
                    "ok": result["ok"],
                    "action": result["arguments"].get("action"),
                    "screen_changed": result["screen_changed"],
                    "retries_used": result["retries_used"],
                },
            )
            return result


def execute_gui_action(
    *,
    action: str,
    dry_run: bool = False,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Execute one GUI action using env or explicit grounding config."""
    resolved_model = (model or os.getenv(DEFAULT_GUI_MODEL_ENV, "")).strip()
    grounding_provider = canonical_provider_name(os.getenv(DEFAULT_GUI_GROUNDING_PROVIDER_ENV, ""))
    grounding_api_key_env = provider_api_key_env(grounding_provider) if grounding_provider else None
    provider_api_key = os.getenv(grounding_api_key_env, "").strip() if grounding_api_key_env else ""
    resolved_api_key = (api_key or provider_api_key).strip()
    resolved_base_url = (base_url or os.getenv(DEFAULT_GUI_BASE_URL_ENV, "")).strip() or None

    if not resolved_model:
        raise ValueError(
            f"Missing GUI model. Set {DEFAULT_GUI_MODEL_ENV} or pass model explicitly."
        )
    if not resolved_api_key:
        if grounding_api_key_env:
            error_hint = grounding_api_key_env
        else:
            error_hint = "provider API key env (for OPENPIPIXIA_GUI_GROUNDING_PROVIDER)"
        raise ValueError(
            f"Missing GUI api key. Set {error_hint} or pass api_key explicitly."
        )

    allow_dangerous_keys = (os.getenv(DEFAULT_GUI_ALLOW_DANGEROUS_KEYS_ENV, "").strip().lower() == "true")
    max_wait_raw = os.getenv(DEFAULT_GUI_MAX_WAIT_SECONDS_ENV, "").strip()
    max_wait_seconds = 5.0
    if max_wait_raw:
        try:
            max_wait_seconds = max(0.0, float(max_wait_raw))
        except ValueError:
            max_wait_seconds = 5.0
    max_parse_retries_raw = os.getenv(DEFAULT_GUI_MAX_PARSE_RETRIES_ENV, "").strip()
    max_parse_retries = 1
    if max_parse_retries_raw:
        try:
            max_parse_retries = max(0, int(max_parse_retries_raw))
        except ValueError:
            max_parse_retries = 1
    verify_screen_change = os.getenv(DEFAULT_GUI_VERIFY_SCREEN_CHANGE_ENV, "true").strip().lower() == "true"
    allowed_actions_raw = os.getenv(DEFAULT_GUI_ALLOWED_ACTIONS_ENV, "").strip()
    blocked_actions_raw = os.getenv(DEFAULT_GUI_BLOCKED_ACTIONS_ENV, "").strip()
    allowed_actions = set([item.strip().lower() for item in allowed_actions_raw.split(",") if item.strip()])
    blocked_actions = set([item.strip().lower() for item in blocked_actions_raw.split(",") if item.strip()])
    max_action_retries_raw = os.getenv(DEFAULT_GUI_MAX_ACTION_RETRIES_ENV, "").strip()
    max_action_retries = 1
    if max_action_retries_raw:
        try:
            max_action_retries = max(0, int(max_action_retries_raw))
        except ValueError:
            max_action_retries = 1

    executor = GroundingExecutor(
        model=resolved_model,
        api_key=resolved_api_key,
        provider=grounding_provider,
        base_url=resolved_base_url,
        runtime=PyAutoGuiRuntime(
            allow_dangerous_keys=allow_dangerous_keys,
            max_wait_seconds=max_wait_seconds,
            allowed_actions=allowed_actions if allowed_actions else None,
            blocked_actions=blocked_actions if blocked_actions else None,
        ),
        max_parse_retries=max_parse_retries,
        verify_screen_change=verify_screen_change,
        max_action_retries=max_action_retries,
    )
    return executor.run(action, dry_run=dry_run)


__all__ = [
    "CapturedScreen",
    "GroundingExecutor",
    "PyAutoGuiRuntime",
    "execute_gui_action",
]
