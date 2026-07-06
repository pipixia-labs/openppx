"""Built-in sandbox permission profiles."""

from __future__ import annotations

from pathlib import Path

from .plan import (
    FileSystemPolicy,
    NetworkMode,
    NetworkPolicy,
    PathAccessMode,
    PathGrant,
    PermissionProfile,
)


def workspace_write_profile(workspace: Path) -> PermissionProfile:
    """Return the default explicit-sandbox profile for a workspace."""
    root = workspace.resolve(strict=False)
    workspace_grant = PathGrant(
        logical_name="workspace",
        host_path=root,
        container_path=str(root),
        access=PathAccessMode.WRITE,
    )
    return PermissionProfile(
        name="workspace-write",
        filesystem=FileSystemPolicy(
            readable_roots=(workspace_grant,),
            writable_roots=(workspace_grant,),
            denied_roots=_default_denied_roots(root),
        ),
        network=NetworkPolicy(mode=NetworkMode.DISABLED),
    )


def read_only_profile(workspace: Path) -> PermissionProfile:
    """Return a workspace read-only sandbox profile."""
    root = workspace.resolve(strict=False)
    workspace_grant = PathGrant(
        logical_name="workspace",
        host_path=root,
        container_path=str(root),
        access=PathAccessMode.READ,
    )
    return PermissionProfile(
        name="read-only",
        filesystem=FileSystemPolicy(
            readable_roots=(workspace_grant,),
            denied_roots=_default_denied_roots(root),
        ),
        network=NetworkPolicy(mode=NetworkMode.DISABLED),
    )


def _default_denied_roots(workspace: Path) -> tuple[Path, ...]:
    return (
        workspace / ".env",
        workspace / ".ssh",
        workspace / ".aws",
        workspace / ".git-credentials",
    )
