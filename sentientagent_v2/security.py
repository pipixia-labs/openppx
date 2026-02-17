"""Unified runtime security policy for tool execution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .env_utils import env_enabled


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    """Runtime security policy shared by file/shell/web tools."""

    workspace_root: Path
    restrict_to_workspace: bool
    strict_mode: bool
    allow_exec: bool
    allow_network: bool
    exec_allowlist: tuple[str, ...]

    def is_exec_allowed(self, command_name: str) -> bool:
        """Return whether the command name is allowed by the policy."""
        if not self.exec_allowlist:
            return True
        return command_name in self.exec_allowlist


class PathGuard:
    """Resolve paths and optionally enforce workspace boundary."""

    def __init__(self, policy: SecurityPolicy):
        self._policy = policy
        self._workspace_root = policy.workspace_root.resolve()

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def resolve_path(self, path: str, *, base_dir: Path | None = None) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            anchor = (base_dir or self._workspace_root).resolve()
            candidate = anchor / candidate
        resolved = candidate.resolve(strict=False)
        self._ensure_allowed(resolved)
        return resolved

    def _ensure_allowed(self, path: Path) -> None:
        if not self._policy.restrict_to_workspace:
            return
        try:
            path.relative_to(self._workspace_root)
        except ValueError as exc:
            raise PermissionError(
                f"Path '{path}' is outside workspace '{self._workspace_root}'"
            ) from exc


def _parse_allowlist(raw_value: str) -> tuple[str, ...]:
    items: list[str] = []
    for token in raw_value.split(","):
        value = token.strip()
        if not value:
            continue
        items.append(value)
    # Keep order and deduplicate.
    return tuple(dict.fromkeys(items))


def _workspace_from_env() -> Path:
    workspace_env = os.getenv("SENTIENTAGENT_V2_WORKSPACE", "").strip()
    if workspace_env:
        return Path(workspace_env).expanduser().resolve()
    return Path.cwd().resolve()


def load_security_policy() -> SecurityPolicy:
    """Load security policy from runtime environment."""
    strict_mode = env_enabled("SENTIENTAGENT_V2_STRICT_MODE", default=False)
    restrict_to_workspace = env_enabled("SENTIENTAGENT_V2_RESTRICT_TO_WORKSPACE", default=False) or strict_mode
    allow_exec = env_enabled("SENTIENTAGENT_V2_ALLOW_EXEC", default=not strict_mode)
    allow_network = env_enabled("SENTIENTAGENT_V2_ALLOW_NETWORK", default=not strict_mode)
    exec_allowlist = _parse_allowlist(os.getenv("SENTIENTAGENT_V2_EXEC_ALLOWLIST", ""))

    return SecurityPolicy(
        workspace_root=_workspace_from_env(),
        restrict_to_workspace=restrict_to_workspace,
        strict_mode=strict_mode,
        allow_exec=allow_exec,
        allow_network=allow_network,
        exec_allowlist=exec_allowlist,
    )


def normalize_allowlist(values: Iterable[object]) -> list[str]:
    """Normalize config-level allowlist items into clean strings."""
    out: list[str] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        out.append(text)
    return list(dict.fromkeys(out))
