"""Workspace-scoped Docker sandbox helpers."""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .docker_backend import DockerSandboxConfig, DockerRunSpec, build_docker_run_spec
from .plan import (
    FileSystemPolicy,
    NetworkMode,
    NetworkPolicy,
    PathAccessMode,
    PathGrant,
    ResourceLimits,
    SandboxCommand,
    SandboxExecutionPlan,
    SandboxMount,
    ValidatedSandboxExecutionPlan,
)
from .profiles import workspace_write_profile
from .validation import resolve_network_mode


@dataclass(frozen=True, slots=True)
class WorkspaceDockerSandbox:
    """Prepared Docker sandbox command metadata for one workspace execution."""

    argv: list[str]
    docker_bin: str
    container_name: str
    timeout_seconds: int
    stdin: str | bytes | None


def build_workspace_docker_sandbox(
    *,
    command_argv: list[str],
    workspace: Path,
    cwd: Path,
    timeout_seconds: float | int | None,
    timeout_cap_seconds: int = 60,
    stdin: str | bytes | None = None,
    env: Mapping[str, str] | None = None,
    labels: Mapping[str, str] | None = None,
    readonly_mounts: Mapping[str, Path] | None = None,
    docker_bin: str | None = None,
    image: str | None = None,
    network_mode: NetworkMode | None = None,
    network_approved: bool = False,
) -> WorkspaceDockerSandbox:
    """Build a validated Docker sandbox command for a workspace execution."""
    root = workspace.resolve(strict=False)
    cap = max(1, int(timeout_cap_seconds))
    profile = workspace_write_profile(root)
    trusted_readonly_mounts = _trusted_readonly_mounts(readonly_mounts or {})
    if trusted_readonly_mounts:
        profile = replace(
            profile,
            filesystem=_filesystem_with_trusted_readonly_mounts(
                profile.filesystem,
                trusted_readonly_mounts,
            ),
        )
    profile = replace(
        profile,
        limits=_resource_limits_from_env(replace(profile.limits, timeout_seconds=cap)),
    )
    network_lock = _network_lock_from_env()
    if network_mode is not None or network_lock is not None:
        resolved_network = resolve_network_mode(
            default_mode=profile.network.mode,
            requested_mode=network_mode,
            lock_mode=network_lock,
            approved=network_approved,
        )
        profile = replace(profile, network=NetworkPolicy(mode=resolved_network, lock=network_lock))
    plan_labels = {
        "openppx.run_id": uuid.uuid4().hex,
        **{str(k): str(v) for k, v in (labels or {}).items()},
    }
    if network_approved:
        plan_labels["openppx.network.approved"] = "1"
    plan = SandboxExecutionPlan(
        command=SandboxCommand(argv=tuple(command_argv)),
        profile=profile,
        mounts=(
            *_workspace_mounts(profile=profile, root=root),
            *_readonly_mounts_from_trusted_paths(trusted_readonly_mounts),
        ),
        env={str(k): str(v) for k, v in (env or {}).items()},
        cwd=str(cwd.resolve(strict=False)),
        stdin=stdin,
        labels=plan_labels,
    )
    validated = ValidatedSandboxExecutionPlan.from_plan(plan)
    resolved_docker_bin = docker_bin or os.getenv("OPENPPX_SANDBOX_DOCKER_BIN", "").strip() or "docker"
    resolved_image = image or os.getenv("OPENPPX_SANDBOX_IMAGE", "").strip() or "openppx-sandbox:dev"
    spec = build_docker_run_spec(
        validated,
        config=DockerSandboxConfig(docker_bin=resolved_docker_bin, image=resolved_image),
    )
    return WorkspaceDockerSandbox(
        argv=list(spec.argv),
        docker_bin=resolved_docker_bin,
        container_name=spec.container_name,
        timeout_seconds=_effective_timeout(timeout_seconds=timeout_seconds, cap_seconds=cap),
        stdin=spec.stdin,
    )


def _trusted_readonly_mounts(raw_mounts: Mapping[str, Path]) -> tuple[tuple[str, Path], ...]:
    items: list[tuple[str, Path]] = []
    for logical_name, path in raw_mounts.items():
        normalized_name = str(logical_name).strip()
        if not normalized_name:
            raise ValueError("trusted readonly mount logical_name must be non-empty")
        items.append((normalized_name, Path(path).resolve(strict=False)))
    return tuple(items)


def _filesystem_with_trusted_readonly_mounts(
    filesystem: FileSystemPolicy,
    mounts: tuple[tuple[str, Path], ...],
) -> FileSystemPolicy:
    grants = tuple(
        PathGrant(
            logical_name=f"backend:{logical_name}",
            host_path=path,
            container_path=str(path),
            access=PathAccessMode.READ,
        )
        for logical_name, path in mounts
    )
    return replace(filesystem, readable_roots=(*filesystem.readable_roots, *grants))


def _readonly_mounts_from_trusted_paths(mounts: tuple[tuple[str, Path], ...]) -> tuple[SandboxMount, ...]:
    return tuple(
        SandboxMount(
            logical_name=f"backend:{logical_name}",
            host_path=path,
            container_path=str(path),
            access=PathAccessMode.READ,
        )
        for logical_name, path in mounts
    )


def cleanup_docker_sandbox_container(docker_bin: str, container_name: str) -> None:
    """Best-effort cleanup for a Docker sandbox container."""
    for args in (["kill", container_name], ["rm", "-f", container_name]):
        try:
            subprocess.run(
                [docker_bin, *args],
                shell=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            continue


def _workspace_mounts(*, profile: object, root: Path) -> tuple[SandboxMount, ...]:
    mounts: list[SandboxMount] = [
        SandboxMount(
            logical_name="workspace",
            host_path=root,
            container_path=str(root),
            access=PathAccessMode.WRITE,
        )
    ]

    git_dir = root / ".git"
    if git_dir.exists() or git_dir.is_symlink():
        mounts.append(
            SandboxMount(
                logical_name="git-metadata",
                host_path=git_dir,
                container_path=str(git_dir),
                access=PathAccessMode.READ,
            )
        )

    for denied in profile.filesystem.denied_roots:
        if denied.exists() or denied.is_symlink():
            mounts.append(
                SandboxMount(
                    logical_name=f"mask:{denied.name}",
                    host_path=Path("/dev/null"),
                    container_path=str(denied),
                    access=PathAccessMode.READ,
                    required=False,
                    mask=True,
                )
            )
    return tuple(mounts)


def _effective_timeout(*, timeout_seconds: float | int | None, cap_seconds: int) -> int:
    if timeout_seconds is None:
        return cap_seconds
    return min(max(1, int(float(timeout_seconds))), cap_seconds)


def _resource_limits_from_env(defaults: ResourceLimits) -> ResourceLimits:
    """Overlay trusted sandbox resource-limit environment configuration."""
    return replace(
        defaults,
        memory=_env_string("OPENPPX_SANDBOX_MEMORY", defaults.memory),
        cpus=_env_float("OPENPPX_SANDBOX_CPUS", defaults.cpus),
        pids_limit=_env_int("OPENPPX_SANDBOX_PIDS_LIMIT", defaults.pids_limit),
        tmpfs_size=_env_string("OPENPPX_SANDBOX_TMPFS_SIZE", defaults.tmpfs_size),
    )


def _network_lock_from_env() -> NetworkMode | None:
    raw = os.getenv("OPENPPX_SANDBOX_NETWORK_LOCK", "").strip().lower()
    if raw in {"1", "true", "yes", "disabled", "none", "no-network", "network-none"}:
        return NetworkMode.DISABLED
    return None


def _env_string(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except Exception:
        return default
    return value if value > 0 else default
