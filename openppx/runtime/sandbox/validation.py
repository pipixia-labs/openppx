"""Validation helpers for sandbox execution plans."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePosixPath

from .plan import (
    NetworkMode,
    PathAccessMode,
    PathGrant,
    SandboxExecutionPlan,
    SandboxMount,
)


class SandboxValidationError(ValueError):
    """Raised when a sandbox execution plan violates policy."""


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WEAKER_BACKEND_RANK = {"none": 0, "bwrap": 1, "docker": 2}
_PROTECTED_CONTAINER_PREFIXES = (
    PurePosixPath("/"),
    PurePosixPath("/etc"),
    PurePosixPath("/usr"),
    PurePosixPath("/bin"),
    PurePosixPath("/proc"),
    PurePosixPath("/sys"),
    PurePosixPath("/dev"),
)
_MASK_SOURCE_ALLOWLIST = (Path("/dev/null"),)


def validate_sandbox_execution_plan(plan: SandboxExecutionPlan) -> None:
    """Validate one sandbox execution plan before a backend can consume it."""
    _validate_command(plan)
    _validate_resources(plan)
    _validate_env(plan)
    _validate_grants(plan)
    _validate_mounts(plan)
    _validate_cwd(plan)
    _validate_network(plan)


def resolve_backend(*, configured_backend: str, requested_backend: str | None = None) -> str:
    """Resolve a sandbox backend without allowing model-requested downgrades."""
    configured = _normalize_backend(configured_backend)
    requested = _normalize_backend(requested_backend or configured)
    if _WEAKER_BACKEND_RANK[requested] < _WEAKER_BACKEND_RANK[configured]:
        raise SandboxValidationError(
            f"sandbox backend downgrade is not allowed: configured={configured}, requested={requested}"
        )
    return requested


def resolve_network_mode(
    *,
    default_mode: NetworkMode,
    requested_mode: NetworkMode | None = None,
    lock_mode: NetworkMode | None = None,
    approved: bool = False,
) -> NetworkMode:
    """Resolve default/requested network policy with an optional hard lock."""
    if lock_mode == NetworkMode.DISABLED:
        return NetworkMode.DISABLED
    requested = requested_mode or default_mode
    if default_mode == NetworkMode.DISABLED and requested == NetworkMode.ENABLED and not approved:
        raise SandboxValidationError("network enablement requires approval")
    if requested == NetworkMode.PROXY_ONLY:
        raise SandboxValidationError("proxy_only network mode is reserved for a later phase")
    return requested


def _validate_command(plan: SandboxExecutionPlan) -> None:
    if not plan.command.argv:
        raise SandboxValidationError("sandbox command argv must be non-empty")
    if any(not item for item in plan.command.argv):
        raise SandboxValidationError("sandbox command argv entries must be non-empty")


def _validate_resources(plan: SandboxExecutionPlan) -> None:
    limits = plan.profile.limits
    if limits.timeout_seconds <= 0:
        raise SandboxValidationError("sandbox timeout_seconds must be positive")
    if limits.output_max_bytes <= 0:
        raise SandboxValidationError("sandbox output_max_bytes must be positive")
    if limits.cpus <= 0:
        raise SandboxValidationError("sandbox cpus must be positive")
    if limits.pids_limit <= 0:
        raise SandboxValidationError("sandbox pids_limit must be positive")
    if not limits.memory.strip() or not limits.tmpfs_size.strip():
        raise SandboxValidationError("sandbox memory and tmpfs_size must be configured")


def _validate_env(plan: SandboxExecutionPlan) -> None:
    policy = plan.profile.env
    allowed = set(policy.allowed_env_names)
    for name, value in plan.env.items():
        if not _ENV_NAME_RE.match(name):
            raise SandboxValidationError(f"invalid sandbox env name: {name!r}")
        if _env_name_denied(name, policy.denied_env_patterns) and name not in allowed:
            raise SandboxValidationError(f"sensitive sandbox env is not allowed: {name}")
        if not isinstance(value, str):
            raise SandboxValidationError(f"sandbox env value must be a string: {name}")


def _validate_grants(plan: SandboxExecutionPlan) -> None:
    for grant in (*plan.profile.filesystem.readable_roots, *plan.profile.filesystem.writable_roots):
        if grant.must_exist and not grant.host_path.exists():
            raise SandboxValidationError(f"sandbox grant path does not exist: {grant.logical_name}")
        if not grant.follow_symlinks and grant.host_path.is_symlink():
            raise SandboxValidationError(f"sandbox grant root cannot be a symlink: {grant.logical_name}")
        _validate_container_path(grant.container_path)


def _validate_mounts(plan: SandboxExecutionPlan) -> None:
    for mount in plan.mounts:
        _validate_container_path(mount.container_path)
        if mount.required and not mount.mask and not mount.host_path.exists():
            raise SandboxValidationError(f"sandbox mount path does not exist: {mount.logical_name}")
        if mount.mask:
            _validate_mask_mount(plan, mount)
            continue
        _validate_same_path_mount(mount)
        _validate_not_denied(plan, mount)
        if not _mount_covered_by_grant(plan, mount):
            raise SandboxValidationError(f"sandbox mount is not covered by profile grants: {mount.logical_name}")


def _validate_cwd(plan: SandboxExecutionPlan) -> None:
    cwd = PurePosixPath(plan.cwd)
    if not cwd.is_absolute():
        raise SandboxValidationError("sandbox cwd must be an absolute container path")
    if not any(_posix_relative_to(cwd, PurePosixPath(mount.container_path)) for mount in plan.mounts):
        raise SandboxValidationError("sandbox cwd must be under a mounted container path")
    denied = tuple(PurePosixPath(str(path.resolve(strict=False))) for path in plan.profile.filesystem.denied_roots)
    if any(_posix_relative_to(cwd, item) for item in denied):
        raise SandboxValidationError("sandbox cwd falls under a denied root")


def _validate_network(plan: SandboxExecutionPlan) -> None:
    policy = plan.profile.network
    if policy.mode == NetworkMode.PROXY_ONLY:
        raise SandboxValidationError("proxy_only network mode is reserved for a later phase")
    if policy.lock == NetworkMode.DISABLED:
        return
    if (
        policy.mode == NetworkMode.ENABLED
        and plan.profile.approval.require_for_network
        and str(plan.labels.get("openppx.network.approved", "")).lower() not in {"1", "true", "approved"}
    ):
        raise SandboxValidationError("network enabled sandbox plan requires approval")


def _validate_container_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if not parsed.is_absolute():
        raise SandboxValidationError(f"sandbox container path must be absolute: {path}")
    for protected in _PROTECTED_CONTAINER_PREFIXES:
        if protected == PurePosixPath("/"):
            if parsed == protected:
                raise SandboxValidationError(f"sandbox container path cannot cover system path: {path}")
            continue
        if parsed == protected or _posix_relative_to(parsed, protected):
            raise SandboxValidationError(f"sandbox container path cannot cover system path: {path}")


def _validate_same_path_mount(mount: SandboxMount) -> None:
    host = mount.host_path.resolve(strict=False)
    container = PurePosixPath(mount.container_path)
    if str(host) != str(container):
        raise SandboxValidationError("docker sandbox mounts must preserve host/container path semantics")


def _validate_not_denied(plan: SandboxExecutionPlan, mount: SandboxMount) -> None:
    host = mount.host_path.resolve(strict=False)
    container = PurePosixPath(mount.container_path)
    for denied in plan.profile.filesystem.denied_roots:
        denied_host = denied.resolve(strict=False)
        denied_container = PurePosixPath(str(denied_host))
        if _path_relative_to(host, denied_host) or _posix_relative_to(container, denied_container):
            raise SandboxValidationError(f"sandbox mount falls under denied root: {mount.logical_name}")


def _validate_mask_mount(plan: SandboxExecutionPlan, mount: SandboxMount) -> None:
    if mount.access != PathAccessMode.READ:
        raise SandboxValidationError("mask mounts must be readonly")
    source = mount.host_path.resolve(strict=False)
    if source not in _MASK_SOURCE_ALLOWLIST:
        raise SandboxValidationError("mask mount source must come from backend allowlist")
    container = PurePosixPath(mount.container_path)
    workspace_mounts = [PurePosixPath(item.container_path) for item in plan.mounts if item.logical_name == "workspace"]
    if not any(_posix_relative_to(container, workspace) for workspace in workspace_mounts):
        raise SandboxValidationError("mask mount target must be under the workspace mount")


def _mount_covered_by_grant(plan: SandboxExecutionPlan, mount: SandboxMount) -> bool:
    grants: tuple[PathGrant, ...]
    if mount.access == PathAccessMode.WRITE:
        grants = plan.profile.filesystem.writable_roots
    else:
        grants = plan.profile.filesystem.readable_roots + plan.profile.filesystem.writable_roots
    return any(_grant_covers_mount(grant, mount) for grant in grants)


def _grant_covers_mount(grant: PathGrant, mount: SandboxMount) -> bool:
    grant_host = grant.host_path.resolve(strict=False)
    mount_host = mount.host_path.resolve(strict=False)
    grant_container = PurePosixPath(grant.container_path)
    mount_container = PurePosixPath(mount.container_path)
    return _path_relative_to(mount_host, grant_host) and _posix_relative_to(mount_container, grant_container)


def _env_name_denied(name: str, patterns: tuple[str, ...]) -> bool:
    upper = name.upper()
    for pattern in patterns:
        candidate = pattern.upper()
        if "*" in candidate or "?" in candidate:
            if fnmatch.fnmatch(upper, candidate):
                return True
            continue
        if candidate in upper:
            return True
    return False


def _normalize_backend(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in _WEAKER_BACKEND_RANK:
        raise SandboxValidationError(f"unknown sandbox backend: {value!r}")
    return normalized


def _path_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _posix_relative_to(path: PurePosixPath, root: PurePosixPath) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
