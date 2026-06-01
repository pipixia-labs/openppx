"""Runtime guard for the supported Google ADK major version."""

from __future__ import annotations

from importlib import metadata

_SUPPORTED_ADK_MAJOR = 2


def _major_version(version: str) -> int | None:
    """Return the leading integer version component when it is parseable."""
    head = (version or "").split(".", 1)[0]
    try:
        return int(head)
    except (TypeError, ValueError):
        return None


def installed_adk_version() -> str:
    """Return the installed google-adk package version."""
    try:
        return metadata.version("google-adk")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError("google-adk is not installed; openppx requires google-adk 2.x.") from exc


def assert_supported_adk_major() -> None:
    """Raise when the installed google-adk major version is unsupported."""
    version = installed_adk_version()
    major = _major_version(version)
    if major != _SUPPORTED_ADK_MAJOR:
        raise RuntimeError(
            "Unsupported google-adk version "
            f"{version!r}; openppx requires google-adk {_SUPPORTED_ADK_MAJOR}.x. "
            "Reinstall dependencies from the project lock file before starting openppx."
        )
