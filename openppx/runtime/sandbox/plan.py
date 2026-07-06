"""Typed sandbox execution plans.

These data structures describe sandbox intent only. They do not execute a
process and do not expose backend flags to callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping


class PathAccessMode(str, Enum):
    """Filesystem access mode for grants and mounts."""

    READ = "read"
    WRITE = "write"


class NetworkMode(str, Enum):
    """Network mode requested for one sandbox execution."""

    DISABLED = "disabled"
    ENABLED = "enabled"
    PROXY_ONLY = "proxy_only"


DEFAULT_DENIED_ENV_PATTERNS: tuple[str, ...] = (
    "*_KEY",
    "*_TOKEN",
    "*_SECRET",
    "*_PASSWORD",
    "AUTH",
    "CREDENTIAL",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "BASH_ENV",
    "ENV",
    "IFS",
    "PYTHONPATH",
)


@dataclass(frozen=True, slots=True)
class PathGrant:
    """A trusted profile-level path capability."""

    logical_name: str
    host_path: Path
    container_path: str
    access: PathAccessMode
    must_exist: bool = True
    follow_symlinks: bool = True


@dataclass(frozen=True, slots=True)
class SandboxMount:
    """One mount required by a sandbox execution plan."""

    logical_name: str
    host_path: Path
    container_path: str
    access: PathAccessMode
    required: bool = True
    mask: bool = False


@dataclass(frozen=True, slots=True)
class FileSystemPolicy:
    """Filesystem policy granted by a permission profile."""

    readable_roots: tuple[PathGrant, ...] = ()
    writable_roots: tuple[PathGrant, ...] = ()
    denied_roots: tuple[Path, ...] = ()
    protect_metadata: bool = True


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Default and ceiling network policy for one profile."""

    mode: NetworkMode = NetworkMode.DISABLED
    lock: NetworkMode | None = None


@dataclass(frozen=True, slots=True)
class EnvPolicy:
    """Environment variable policy for sandbox execution."""

    inherit_host_env: bool = False
    allowed_env_names: tuple[str, ...] = ()
    denied_env_patterns: tuple[str, ...] = DEFAULT_DENIED_ENV_PATTERNS


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Resource limits for one sandbox execution profile."""

    timeout_seconds: int = 60
    output_max_bytes: int = 2 * 1024 * 1024
    memory: str = "1024m"
    cpus: float = 2.0
    pids_limit: int = 256
    tmpfs_size: str = "256m"


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Policy knobs that mark executions as requiring human approval."""

    require_for_network: bool = True
    require_for_extra_mounts: bool = True
    require_for_backend_downgrade: bool = True
    require_for_danger_full_access: bool = True


@dataclass(frozen=True, slots=True)
class PermissionProfile:
    """Named sandbox permission profile."""

    name: str
    filesystem: FileSystemPolicy
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    env: EnvPolicy = field(default_factory=EnvPolicy)
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    approval: ApprovalPolicy = field(default_factory=ApprovalPolicy)


@dataclass(frozen=True, slots=True)
class SandboxCommand:
    """Structured command to execute inside a sandbox backend."""

    argv: tuple[str, ...]
    shell: bool = False


@dataclass(frozen=True, slots=True)
class SandboxExecutionPlan:
    """One unvalidated sandbox execution plan."""

    command: SandboxCommand
    profile: PermissionProfile
    mounts: tuple[SandboxMount, ...]
    env: Mapping[str, str]
    cwd: str
    stdin: str | bytes | None = None
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidatedSandboxExecutionPlan:
    """Sandbox execution plan that passed validation at construction time."""

    plan: SandboxExecutionPlan

    def __post_init__(self) -> None:
        from .validation import validate_sandbox_execution_plan

        validate_sandbox_execution_plan(self.plan)

    @classmethod
    def from_plan(cls, plan: SandboxExecutionPlan) -> "ValidatedSandboxExecutionPlan":
        """Validate and wrap a sandbox execution plan."""
        return cls(plan)
