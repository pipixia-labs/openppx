"""Prompt loading helpers for GUI planner/executor."""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_GUI_EXECUTOR_SYSTEM_PROMPT_PATH_ENV = "OPENPIPIXIA_GUI_EXECUTOR_SYSTEM_PROMPT_PATH"
DEFAULT_GUI_PLANNER_SYSTEM_PROMPT_PATH_ENV = "OPENPIPIXIA_GUI_PLANNER_SYSTEM_PROMPT_PATH"


def _read_prompt_file(path: Path) -> str:
    """Read one UTF-8 prompt file and validate non-empty content."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"prompt file is empty: {path}")
    return text


def _load_prompt(*, filename: str, override_env: str) -> str:
    """Load prompt from env override path or package-local prompt file."""
    override = os.getenv(override_env, "").strip()
    if override:
        return _read_prompt_file(Path(override).expanduser())
    prompt_path = Path(__file__).with_name("prompts") / filename
    return _read_prompt_file(prompt_path)


def load_executor_system_prompt() -> str:
    """Load system prompt for single-step GUI grounding executor."""
    return _load_prompt(
        filename="executor_system_prompt.md",
        override_env=DEFAULT_GUI_EXECUTOR_SYSTEM_PROMPT_PATH_ENV,
    )


def load_planner_system_prompt() -> str:
    """Load system prompt for multi-step GUI task planner."""
    return _load_prompt(
        filename="planner_system_prompt.md",
        override_env=DEFAULT_GUI_PLANNER_SYSTEM_PROMPT_PATH_ENV,
    )


__all__ = [
    "DEFAULT_GUI_EXECUTOR_SYSTEM_PROMPT_PATH_ENV",
    "DEFAULT_GUI_PLANNER_SYSTEM_PROMPT_PATH_ENV",
    "load_executor_system_prompt",
    "load_planner_system_prompt",
]
