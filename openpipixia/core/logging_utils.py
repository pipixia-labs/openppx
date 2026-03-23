"""Logging helpers with a Loguru-first backend."""

from __future__ import annotations

import json
import os
from typing import Any

from loguru import logger

from .env_utils import env_enabled


def debug_logging_enabled() -> bool:
    """Return whether debug logging is enabled globally."""
    return env_enabled("OPENPIPIXIA_DEBUG", default=False)


def debug_body(payload: Any) -> str:
    """Serialize debug payloads for stable structured log lines."""
    try:
        if isinstance(payload, str):
            return payload
        if isinstance(payload, (dict, list)):
            return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return str(payload)


def emit_debug(tag: str, payload: Any, *, depth: int = 2) -> None:
    """Emit a debug line in a consistent format."""
    body = debug_body(payload)

    # Default `depth=2` points to the original caller above local `_debug` wrappers.
    logger.opt(depth=depth).debug("[DEBUG] {}: {}", tag, body)
    debug_log_path = os.getenv("OPENPIPIXIA_DEBUG_LOG_PATH", "").strip()
    if debug_log_path:
        try:
            with open(debug_log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[DEBUG] {tag}: {body}\n")
        except Exception:
            # Do not break main flow when debug-file sink is unavailable.
            return
