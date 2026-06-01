"""ADK RunConfig helpers for openppx runtime profiles."""

from __future__ import annotations

from typing import Any, Literal

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.sessions.base_session_service import GetSessionConfig

RunConfigProfile = Literal["full", "ephemeral"]

_DEFAULT_FULL_MAX_LLM_CALLS = 500
_DEFAULT_EPHEMERAL_MAX_LLM_CALLS = 8


def _normalize_profile(profile: str | None) -> RunConfigProfile:
    """Normalize one run-config profile name."""
    normalized = (profile or "full").strip().lower()
    if normalized in {"full", "ephemeral"}:
        return normalized
    raise ValueError(f"unsupported run config profile {profile!r}; expected 'full' or 'ephemeral'")


def build_run_config(
    *,
    profile: str = "full",
    streaming: bool = False,
    max_llm_calls: int | None = None,
    num_recent_events: int | None = None,
    custom_metadata: dict[str, Any] | None = None,
    save_input_blobs_as_artifacts: bool | None = None,
) -> RunConfig:
    """Build one ADK RunConfig from an openppx runtime profile."""
    resolved_profile = _normalize_profile(profile)
    if max_llm_calls is None:
        max_llm_calls = (
            _DEFAULT_EPHEMERAL_MAX_LLM_CALLS
            if resolved_profile == "ephemeral"
            else _DEFAULT_FULL_MAX_LLM_CALLS
        )
    if num_recent_events is None and resolved_profile == "ephemeral":
        num_recent_events = 0
    if save_input_blobs_as_artifacts is None:
        save_input_blobs_as_artifacts = False

    metadata: dict[str, Any] = {"profile": resolved_profile}
    metadata.update(custom_metadata or {})

    return RunConfig(
        streaming_mode=StreamingMode.SSE if streaming else StreamingMode.NONE,
        max_llm_calls=max_llm_calls,
        custom_metadata=metadata,
        get_session_config=(
            GetSessionConfig(num_recent_events=num_recent_events)
            if num_recent_events is not None
            else None
        ),
        save_input_blobs_as_artifacts=save_input_blobs_as_artifacts,
    )
