"""Runtime helpers for gateway execution.

Keep this package init lightweight so importing submodules (for example,
``openppx.runtime.cron_service``) does not eagerly pull ADK/session stacks.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "AccessPolicy",
    "AgentAccessStore",
    "ArtifactConfig",
    "ClientApiClient",
    "MemoryQueryService",
    "MemoryConfig",
    "SessionConfig",
    "create_agent_access_store",
    "create_artifact_service",
    "create_memory_service",
    "create_runner",
    "create_session_service",
    "extract_text",
    "run_text_async",
    "create_identity_store",
    "load_artifact_config",
    "load_agent_access_store_config",
    "load_memory_config",
    "load_session_config",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AccessPolicy": ("openppx.runtime.access_policy", "AccessPolicy"),
    "AgentAccessStore": ("openppx.runtime.agent_access_store", "AgentAccessStore"),
    "extract_text": ("openppx.runtime.adk_utils", "extract_text"),
    "run_text_async": ("openppx.runtime.adk_utils", "run_text_async"),
    "ArtifactConfig": ("openppx.runtime.artifact_service", "ArtifactConfig"),
    "ClientApiClient": ("openppx.runtime.client_api_client", "ClientApiClient"),
    "create_agent_access_store": ("openppx.runtime.agent_access_store", "create_agent_access_store"),
    "create_artifact_service": ("openppx.runtime.artifact_service", "create_artifact_service"),
    "create_identity_store": ("openppx.runtime.identity_store", "create_identity_store"),
    "load_artifact_config": ("openppx.runtime.artifact_service", "load_artifact_config"),
    "load_agent_access_store_config": ("openppx.runtime.agent_access_store", "load_agent_access_store_config"),
    "MemoryQueryService": ("openppx.runtime.memory_query_service", "MemoryQueryService"),
    "MemoryConfig": ("openppx.runtime.memory_service", "MemoryConfig"),
    "create_memory_service": ("openppx.runtime.memory_service", "create_memory_service"),
    "load_memory_config": ("openppx.runtime.memory_service", "load_memory_config"),
    "create_runner": ("openppx.runtime.runner_factory", "create_runner"),
    "SessionConfig": ("openppx.runtime.session_service", "SessionConfig"),
    "create_session_service": ("openppx.runtime.session_service", "create_session_service"),
    "load_session_config": ("openppx.runtime.session_service", "load_session_config"),
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
