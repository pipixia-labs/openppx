"""Per-agent auth/token storage path helpers."""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_AGENT_ID = "main"


def _default_agent_dir() -> Path:
    return (Path.home() / ".openheron" / "agents" / _DEFAULT_AGENT_ID).resolve()


def resolve_current_agent_dir() -> Path:
    """Resolve current agentDir from runtime context/env/default path."""

    try:
        from ..runtime.agent_runtime import get_current_agent_runtime

        runtime = get_current_agent_runtime()
        if runtime is not None:
            return runtime.agent_dir.resolve(strict=False)
    except Exception:
        pass

    env_value = os.getenv("OPENHERON_AGENT_DIR", "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve(strict=False)
    return _default_agent_dir()


def resolve_openai_codex_oauth_data_dir(agent_dir: Path | None = None) -> Path:
    """Return oauth-cli-kit data dir for OpenAI Codex under one agentDir."""

    root = (agent_dir or resolve_current_agent_dir()).expanduser().resolve(strict=False)
    return (root / "auth" / "openai_codex" / "oauth_cli_kit").resolve(strict=False)


def resolve_github_copilot_token_dir(agent_dir: Path | None = None) -> Path:
    """Return GitHub Copilot token cache dir under one agentDir."""

    root = (agent_dir or resolve_current_agent_dir()).expanduser().resolve(strict=False)
    return (root / "auth" / "github_copilot").resolve(strict=False)
