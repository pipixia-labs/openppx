"""Shared environment/config flag helpers."""

from __future__ import annotations

import os
from typing import Any

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def is_enabled(value: Any, *, default: bool = False) -> bool:
    """Interpret common truthy values from config/env payloads."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_VALUES
    return default


def env_enabled(name: str, *, default: bool = False) -> bool:
    """Read an env var and parse it as a boolean flag."""
    return is_enabled(os.getenv(name), default=default)
