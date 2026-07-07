"""Shared parsing for sandbox options embedded in declarative API recipes."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .plan import NetworkMode
from .validation import resolve_backend


_IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,254}$")
_DISABLED_BACKEND_VALUES = {"", "0", "false", "none", "off"}


@dataclass(frozen=True, slots=True)
class RecipeSandboxOptions:
    """Trusted sandbox options resolved from one declarative API recipe."""

    backend: str
    network_mode: NetworkMode = NetworkMode.DISABLED
    network_approved: bool = False
    image: str | None = None

    @property
    def labels(self) -> dict[str, str]:
        """Return audit labels implied by the resolved sandbox options."""
        labels: dict[str, str] = {}
        if self.network_approved:
            labels["openppx.network.approved"] = "1"
        if self.image:
            labels["openppx.image.approved"] = "1"
        return labels


def recipe_sandbox_declared(raw: Any) -> bool:
    """Return whether a recipe sandbox value requests any sandbox backend."""
    if raw in (None, False):
        return False
    if isinstance(raw, str) and raw.strip().lower() in {"", "0", "false", "none", "off"}:
        return False
    if isinstance(raw, dict):
        return bool(raw.get("required", False) or str(raw.get("backend", "")).strip())
    return True


def resolve_recipe_sandbox_options(
    raw: Any,
    *,
    runner_name: str,
    env: Mapping[str, str],
    default_backend: str | None = None,
) -> RecipeSandboxOptions | None:
    """Resolve recipe sandbox options without trusting model-controlled relaxation.

    ``default_backend`` is trusted runtime policy. Recipe-controlled disabled
    values are treated as absence when a default backend exists, so a modified
    skill cannot opt out of a sandbox policy selected by the host.
    """
    trusted_default = _normalize_default_backend(default_backend)
    sandbox_options: dict[str, Any]
    if raw is None or raw is False:
        requested = trusted_default
        sandbox_options = {}
    elif isinstance(raw, str) and _is_disabled_backend(raw):
        requested = trusted_default or "none"
        sandbox_options = {}
    elif raw is True:
        requested = "docker"
        sandbox_options = {}
    elif isinstance(raw, str):
        requested = raw.strip().lower()
        sandbox_options = {}
    elif isinstance(raw, dict):
        sandbox_options = dict(raw)
        required = bool(raw.get("required", False))
        backend_value = raw.get("backend")
        backend = str(backend_value or "").strip().lower()
        if backend_value is not None and _is_disabled_backend(backend):
            requested = trusted_default or "none"
        else:
            requested = backend or ("docker" if required else trusted_default)
    else:
        raise ValueError(f"{runner_name} API recipe sandbox must be a string, boolean, or object")
    if not requested:
        return None

    configured = trusted_default or env.get("OPENPPX_SANDBOX_BACKEND", "").strip().lower() or "none"
    if requested == "none" and configured == "none":
        return None
    backend = resolve_backend(configured_backend=configured, requested_backend=requested)
    if backend != "docker":
        raise ValueError(f"{runner_name} API sandbox currently supports only docker")

    network_mode, network_approved = _resolve_network_mode(
        sandbox_options.get("network", "disabled"),
        runner_name=runner_name,
        env=env,
    )
    image = _resolve_image(
        sandbox_options.get("image"),
        runner_name=runner_name,
        env=env,
    )
    return RecipeSandboxOptions(
        backend=backend,
        network_mode=network_mode,
        network_approved=network_approved,
        image=image,
    )


def _resolve_network_mode(
    raw: Any,
    *,
    runner_name: str,
    env: Mapping[str, str],
) -> tuple[NetworkMode, bool]:
    normalized = str(raw or "disabled").strip().lower()
    if normalized in {"", "0", "false", "none", "off", "disabled"}:
        return NetworkMode.DISABLED, False
    if normalized in {"1", "true", "on", "enabled", "bridge"}:
        if _network_locked_disabled(env.get("OPENPPX_SANDBOX_NETWORK_LOCK")):
            raise ValueError(
                f"{runner_name} API sandbox network enablement is disabled by OPENPPX_SANDBOX_NETWORK_LOCK"
            )
        if not _truthy(env.get("OPENPPX_SANDBOX_ALLOW_NETWORK")):
            raise ValueError(
                f"{runner_name} API sandbox network enablement requires OPENPPX_SANDBOX_ALLOW_NETWORK=1"
            )
        return NetworkMode.ENABLED, True
    if normalized in {"proxy", "proxy_only", "proxy-only"}:
        raise ValueError(f"{runner_name} API sandbox proxy_only network mode is not implemented")
    raise ValueError(f"{runner_name} API sandbox network mode is invalid: {raw!r}")


def _resolve_image(raw: Any, *, runner_name: str, env: Mapping[str, str]) -> str | None:
    image = str(raw or "").strip()
    if not image:
        return None
    if not _IMAGE_REF_RE.match(image):
        raise ValueError(f"{runner_name} API sandbox.image is not a valid Docker image reference")

    default_image = env.get("OPENPPX_SANDBOX_IMAGE", "").strip() or "openppx-sandbox:dev"
    if image == default_image:
        return image
    trusted_patterns = _split_csv(env.get("OPENPPX_SANDBOX_TRUSTED_IMAGES", ""))
    if any(fnmatch.fnmatchcase(image, pattern) for pattern in trusted_patterns):
        return image
    raise ValueError(f"{runner_name} API sandbox.image requires OPENPPX_SANDBOX_TRUSTED_IMAGES allowlist")


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on", "enabled", "allow", "allowed"}


def _normalize_default_backend(raw: str | None) -> str:
    normalized = str(raw or "").strip().lower()
    return "" if _is_disabled_backend(normalized) else normalized


def _is_disabled_backend(raw: Any) -> bool:
    return str(raw or "").strip().lower() in _DISABLED_BACKEND_VALUES


def _network_locked_disabled(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "disabled",
        "none",
        "no-network",
        "network-none",
    }


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())
