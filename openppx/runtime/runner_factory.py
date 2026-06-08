"""Runner construction helpers shared by CLI and gateway."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from google.adk.apps.app import App, EventsCompactionConfig, ResumabilityConfig
from google.adk.plugins.save_files_as_artifacts_plugin import SaveFilesAsArtifactsPlugin
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from .adk_version import assert_supported_adk_major
from .artifact_service import create_artifact_service
from .context_cache import build_context_cache_config
from .debug_callbacks import build_openppx_llm_plugins
from .long_task_context import LongTaskContextPlugin
from .memory_service import create_memory_service
from .memory_ingest_plugin import OpenPpxMemoryIngestPlugin
from .runner_profiles import RunnerProfile
from .session_service import create_session_service
from .step_events import OpenPpxStepEventPlugin
from .workspace_bootstrap import OpenPpxWorkspaceBootstrapPlugin


@dataclass(frozen=True, slots=True)
class RunnerProfilePolicy:
    """Lifecycle policy for one runner profile.

    This keeps memory, workspace bootstrap, artifact persistence, and ADK
    runtime services explicit instead of scattering those decisions across
    ``create_runner`` branches.
    """

    profile: RunnerProfile
    persistent_session: bool
    default_memory_service: bool
    default_artifact_service: bool
    enable_step_events: bool
    enable_memory_ingest: bool
    enable_workspace_bootstrap: bool
    enable_long_task_context: bool
    enable_model_callbacks: bool
    enable_input_file_artifacts: bool
    enable_resumability: bool
    enable_events_compaction: bool
    enable_context_cache: bool


def _parse_enabled(raw: str | None, *, default: bool) -> bool:
    """Parse common truthy/falsey env values with a deterministic fallback."""
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if not normalized:
        return default
    return normalized not in {"0", "false", "off", "no"}


def _parse_non_negative_int(raw: str | None, *, default: int) -> int:
    """Parse a non-negative integer with a fallback value."""
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except Exception:
        return default
    return value if value >= 0 else default


def _parse_positive_int(raw: str | None) -> int | None:
    """Parse a strictly positive integer from env input."""
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        value = int(stripped)
    except Exception:
        return None
    return value if value > 0 else None


def _build_events_compaction_config() -> EventsCompactionConfig | None:
    """Build ADK events compaction config from environment variables."""
    enabled = _parse_enabled(
        os.getenv("OPENPPX_COMPACTION_ENABLED"),
        default=True,
    )
    if not enabled:
        return None

    interval = _parse_non_negative_int(
        os.getenv("OPENPPX_COMPACTION_INTERVAL"),
        default=8,
    )
    overlap = _parse_non_negative_int(
        os.getenv("OPENPPX_COMPACTION_OVERLAP"),
        default=1,
    )

    kwargs: dict[str, Any] = {
        "compaction_interval": max(1, interval),
        "overlap_size": overlap,
    }

    token_threshold = _parse_positive_int(os.getenv("OPENPPX_COMPACTION_TOKEN_THRESHOLD"))
    event_retention_size = _parse_non_negative_int(
        os.getenv("OPENPPX_COMPACTION_EVENT_RETENTION"),
        default=-1,
    )
    if event_retention_size < 0:
        event_retention_size = None

    # ADK requires token_threshold and event_retention_size to be configured as
    # a pair; ignore partial input to avoid startup failure.
    if token_threshold is not None and event_retention_size is not None:
        kwargs["token_threshold"] = token_threshold
        kwargs["event_retention_size"] = event_retention_size

    return EventsCompactionConfig(**kwargs)


def _normalize_runner_profile(profile: str | None) -> RunnerProfile:
    """Normalize one runner profile name."""
    normalized = (profile or "full").strip().lower()
    if normalized in {"full", "ephemeral"}:
        return normalized
    raise ValueError(f"unsupported runner profile {profile!r}; expected 'full' or 'ephemeral'")


def _runner_profile_policy(profile: RunnerProfile) -> RunnerProfilePolicy:
    """Return the lifecycle policy for a normalized runner profile."""
    if profile == "full":
        return RunnerProfilePolicy(
            profile="full",
            persistent_session=True,
            default_memory_service=True,
            default_artifact_service=True,
            enable_step_events=True,
            enable_memory_ingest=True,
            enable_workspace_bootstrap=True,
            enable_long_task_context=True,
            enable_model_callbacks=True,
            enable_input_file_artifacts=True,
            enable_resumability=True,
            enable_events_compaction=True,
            enable_context_cache=True,
        )
    if profile == "ephemeral":
        return RunnerProfilePolicy(
            profile="ephemeral",
            persistent_session=False,
            default_memory_service=False,
            default_artifact_service=False,
            enable_step_events=False,
            enable_memory_ingest=False,
            enable_workspace_bootstrap=False,
            enable_long_task_context=False,
            enable_model_callbacks=False,
            enable_input_file_artifacts=False,
            enable_resumability=False,
            enable_events_compaction=False,
            enable_context_cache=False,
        )
    raise AssertionError(f"unhandled runner profile {profile!r}")


def _target_agent_name(agent: Any) -> str | None:
    """Return the agent name used to preserve root-callback scoping."""
    name = getattr(agent, "name", None)
    return name if isinstance(name, str) and name else None


def _build_profile_plugins(*, agent: Any, policy: RunnerProfilePolicy) -> list[Any]:
    """Build plugins according to one profile lifecycle policy."""
    target_agent_name = _target_agent_name(agent)
    plugins: list[Any] = []
    if policy.enable_step_events:
        plugins.append(OpenPpxStepEventPlugin())
    if policy.enable_memory_ingest:
        plugins.append(OpenPpxMemoryIngestPlugin(target_agent_name=target_agent_name))
    if policy.enable_workspace_bootstrap:
        plugins.append(OpenPpxWorkspaceBootstrapPlugin(target_agent_name=target_agent_name))
    if policy.enable_long_task_context:
        plugins.append(LongTaskContextPlugin(target_agent_name=target_agent_name))
    if policy.enable_model_callbacks:
        plugins.extend(
            build_openppx_llm_plugins(
                profile=policy.profile,
                target_agent_name=target_agent_name,
            )
        )
    if policy.enable_input_file_artifacts:
        plugins.append(SaveFilesAsArtifactsPlugin())
    return plugins


def _build_profile_app(*, agent: Any, app_name: str, policy: RunnerProfilePolicy) -> App:
    """Build an ADK App according to one profile lifecycle policy."""
    resumability_config = None
    if policy.enable_resumability:
        resumability_config = ResumabilityConfig(is_resumable=True)

    events_compaction_config = None
    if policy.enable_events_compaction:
        events_compaction_config = _build_events_compaction_config()

    context_cache_config = None
    if policy.enable_context_cache:
        context_cache_config = build_context_cache_config(profile=policy.profile)

    return App(
        name=app_name,
        root_agent=agent,
        resumability_config=resumability_config,
        events_compaction_config=events_compaction_config,
        context_cache_config=context_cache_config,
        plugins=_build_profile_plugins(agent=agent, policy=policy),
    )


def _build_session_service(*, policy: RunnerProfilePolicy, explicit_service: Any | None) -> Any:
    """Build or reuse the session service for one profile."""
    if explicit_service is not None:
        return explicit_service
    if policy.persistent_session:
        return create_session_service()
    return InMemorySessionService()


def _build_memory_service(*, policy: RunnerProfilePolicy, explicit_service: Any | None) -> Any | None:
    """Build or reuse the memory service for one profile."""
    if explicit_service is not None:
        return explicit_service
    if policy.default_memory_service:
        return create_memory_service()
    return None


def _build_artifact_service(*, policy: RunnerProfilePolicy, explicit_service: Any | None) -> Any | None:
    """Build or reuse the artifact service for one profile."""
    if explicit_service is not None:
        return explicit_service
    if policy.default_artifact_service:
        return create_artifact_service()
    return None


def create_runner(
    *,
    agent: Any,
    app_name: str,
    profile: str = "full",
    session_service: Any | None = None,
    memory_service: Any | None = None,
    artifact_service: Any | None = None,
) -> tuple[Runner, Any]:
    """Create a runner with a shared session service contract.

    The runner is created from an ADK ``App`` so we can enable resumability and
    context compaction with project-level defaults.

    Memory is process-shared by default (single runner instance), so the same
    ``user_id`` can retrieve long-term memory across multiple sessions while
    preserving user isolation in ADK memory scope.
    """
    assert_supported_adk_major()
    resolved_profile = _normalize_runner_profile(profile)
    policy = _runner_profile_policy(resolved_profile)
    service = _build_session_service(policy=policy, explicit_service=session_service)
    memory = _build_memory_service(policy=policy, explicit_service=memory_service)
    artifacts = _build_artifact_service(policy=policy, explicit_service=artifact_service)
    app = _build_profile_app(agent=agent, app_name=app_name, policy=policy)

    runner = Runner(
        app=app,
        app_name=app_name,
        artifact_service=artifacts,
        session_service=service,
        memory_service=memory,
        auto_create_session=True,
    )
    return runner, service
