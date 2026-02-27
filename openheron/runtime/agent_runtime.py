"""Per-request agent runtime context.

This module carries resolved per-agent capabilities (workspace, security,
tool permissions, skill allowlist) through async execution via ContextVar.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True, slots=True)
class AgentRuntimeContext:
    """Resolved runtime context for one routed agent request."""

    agent_id: str
    workspace_root: Path
    agent_dir: Path
    allow_exec: bool = True
    allow_network: bool = True
    restrict_to_workspace: bool = False
    exec_allowlist: tuple[str, ...] = ()
    tools_allow: tuple[str, ...] = ()
    tools_deny: tuple[str, ...] = ()
    skills_allow: tuple[str, ...] = ()
    fs_allowed_paths: tuple[Path, ...] = ()
    fs_deny_paths: tuple[Path, ...] = ()
    fs_read_only_paths: tuple[Path, ...] = ()
    fs_workspace_only: bool = False
    system_permissions: dict[str, bool] = field(default_factory=dict)
    heartbeat: dict[str, object] = field(default_factory=dict)


_CURRENT_AGENT_RUNTIME: ContextVar[AgentRuntimeContext | None] = ContextVar(
    "openheron_current_agent_runtime",
    default=None,
)


def get_current_agent_runtime() -> AgentRuntimeContext | None:
    """Return the current agent runtime context, if any."""

    return _CURRENT_AGENT_RUNTIME.get()


@contextmanager
def agent_runtime_context(agent_runtime: AgentRuntimeContext) -> Iterator[None]:
    """Set current agent runtime context within one request scope."""

    token: Token[AgentRuntimeContext | None] = _CURRENT_AGENT_RUNTIME.set(agent_runtime)
    previous_agent_dir = os.environ.get("OPENHERON_AGENT_DIR")
    previous_copilot_token_dir = os.environ.get("GITHUB_COPILOT_TOKEN_DIR")
    os.environ["OPENHERON_AGENT_DIR"] = str(agent_runtime.agent_dir)
    os.environ["GITHUB_COPILOT_TOKEN_DIR"] = str(
        agent_runtime.agent_dir / "auth" / "github_copilot"
    )
    try:
        yield
    finally:
        if previous_agent_dir is None:
            os.environ.pop("OPENHERON_AGENT_DIR", None)
        else:
            os.environ["OPENHERON_AGENT_DIR"] = previous_agent_dir
        if previous_copilot_token_dir is None:
            os.environ.pop("GITHUB_COPILOT_TOKEN_DIR", None)
        else:
            os.environ["GITHUB_COPILOT_TOKEN_DIR"] = previous_copilot_token_dir
        _CURRENT_AGENT_RUNTIME.reset(token)
