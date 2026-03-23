"""Runtime helpers for gateway execution.

Keep this package init lightweight so importing submodules (for example,
``openpipixia.runtime.cron_service``) does not eagerly pull ADK/session stacks.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "MemoryConfig",
    "SessionConfig",
    "create_memory_service",
    "create_runner",
    "create_session_service",
    "extract_text",
    "load_memory_config",
    "load_session_config",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "extract_text": ("openpipixia.runtime.adk_utils", "extract_text"),
    "MemoryConfig": ("openpipixia.runtime.memory_service", "MemoryConfig"),
    "create_memory_service": ("openpipixia.runtime.memory_service", "create_memory_service"),
    "load_memory_config": ("openpipixia.runtime.memory_service", "load_memory_config"),
    "create_runner": ("openpipixia.runtime.runner_factory", "create_runner"),
    "SessionConfig": ("openpipixia.runtime.session_service", "SessionConfig"),
    "create_session_service": ("openpipixia.runtime.session_service", "create_session_service"),
    "load_session_config": ("openpipixia.runtime.session_service", "load_session_config"),
}


def __getattr__(name: str) -> Any:
    """Resolve runtime exports lazily on first access."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
