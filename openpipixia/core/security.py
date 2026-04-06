"""Unified runtime security policy for tool execution."""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from .env_utils import env_enabled

_LOCAL_HOSTS = {"localhost", "localhost.localdomain"}


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    """Runtime security policy shared by file/shell/web tools."""

    workspace_root: Path
    restrict_to_workspace: bool
    filesystem_access: str
    allow_exec: bool
    allow_network: bool
    exec_allowlist: tuple[str, ...]

    def is_exec_allowed(self, command_name: str) -> bool:
        """Return whether the command name is allowed by the policy."""
        if not self.exec_allowlist:
            return True
        return command_name in self.exec_allowlist

    @property
    def can_write_files(self) -> bool:
        """Return whether file mutations are allowed by policy."""
        return self.filesystem_access == "read_write"


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
    workspace_env = os.getenv("OPENPIPIXIA_WORKSPACE", "").strip()
    if workspace_env:
        return Path(workspace_env).expanduser().resolve()
    return Path.cwd().resolve()


def load_security_policy() -> SecurityPolicy:
    """Load security policy from runtime environment."""
    restrict_to_workspace = env_enabled("OPENPIPIXIA_RESTRICT_TO_WORKSPACE", default=False)
    filesystem_access = os.getenv("OPENPIPIXIA_FILESYSTEM_ACCESS", "read_write").strip().lower() or "read_write"
    if filesystem_access not in {"read_only", "read_write"}:
        filesystem_access = "read_write"
    allow_exec = env_enabled("OPENPIPIXIA_ALLOW_EXEC", default=True)
    allow_network = env_enabled("OPENPIPIXIA_ALLOW_NETWORK", default=True)
    exec_allowlist = _parse_allowlist(os.getenv("OPENPIPIXIA_EXEC_ALLOWLIST", ""))

    return SecurityPolicy(
        workspace_root=_workspace_from_env(),
        restrict_to_workspace=restrict_to_workspace,
        filesystem_access=filesystem_access,
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


def is_private_or_local_ip(raw_ip: str) -> bool:
    """Return whether one IP text points to private/local/reserved space."""

    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_network_hostname(
    hostname: str,
    *,
    block_private_env: str = "OPENPIPIXIA_BROWSER_BLOCK_PRIVATE_NETWORKS",
    block_private_default: bool = True,
    block_dns_env: str = "OPENPIPIXIA_BROWSER_BLOCK_PRIVATE_DNS",
    block_dns_default: bool = False,
) -> str | None:
    """Validate one host against private-network policy.

    Returns an error string when blocked, otherwise ``None``.
    """

    if not env_enabled(block_private_env, default=block_private_default):
        return None

    normalized = hostname.strip().lower()
    if not normalized:
        return None
    if normalized in _LOCAL_HOSTS or normalized.endswith(".localhost"):
        return "target host is blocked by policy (private-network policy)"
    if is_private_or_local_ip(normalized):
        return "target host is blocked by policy (private-network policy)"

    if not env_enabled(block_dns_env, default=block_dns_default):
        return None

    try:
        infos = socket.getaddrinfo(normalized, None)
    except OSError:
        return None
    for info in infos:
        sockaddr = info[4]
        ip_text = str(sockaddr[0]) if isinstance(sockaddr, tuple) and sockaddr else ""
        if ip_text and is_private_or_local_ip(ip_text):
            return "target host is blocked by policy (private-network policy)"
    return None


def validate_network_url(
    url: str,
    *,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
    require_host: bool = True,
    block_private_env: str = "OPENPIPIXIA_BROWSER_BLOCK_PRIVATE_NETWORKS",
    block_private_default: bool = True,
    block_dns_env: str = "OPENPIPIXIA_BROWSER_BLOCK_PRIVATE_DNS",
    block_dns_default: bool = False,
) -> str | None:
    """Validate one outbound network URL including private-network policy."""

    try:
        parsed = urlparse(url)
    except Exception as exc:
        return str(exc)
    if parsed.scheme not in allowed_schemes:
        return f"Only {'/'.join(allowed_schemes)} URLs are supported."
    if require_host and parsed.scheme in {"http", "https"} and not parsed.netloc:
        return "URL must include a domain."
    if parsed.scheme in {"http", "https"}:
        return validate_network_hostname(
            parsed.hostname or "",
            block_private_env=block_private_env,
            block_private_default=block_private_default,
            block_dns_env=block_dns_env,
            block_dns_default=block_dns_default,
        )
    return None
