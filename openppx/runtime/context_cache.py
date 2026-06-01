"""Context-cache configuration for openppx.

ADK 2.1 explicit context caching is App-level and caches more than the static
root policy: system instruction, tools, tool config, and part of request
contents. Keep it opt-in and full-profile-only.
"""

from __future__ import annotations

import os

from google.adk.agents.context_cache_config import ContextCacheConfig

from .runner_profiles import RunnerProfile

_CONTEXT_CACHE_ENABLED_ENV = "OPENPPX_CONTEXT_CACHE_ENABLED"
_CONTEXT_CACHE_INTERVALS_ENV = "OPENPPX_CONTEXT_CACHE_INTERVALS"
_CONTEXT_CACHE_MIN_TOKENS_ENV = "OPENPPX_CONTEXT_CACHE_MIN_TOKENS"
_CONTEXT_CACHE_TTL_SECONDS_ENV = "OPENPPX_CONTEXT_CACHE_TTL_SECONDS"

_DEFAULT_CACHE_INTERVALS = 5
_DEFAULT_MIN_TOKENS = 4096
_DEFAULT_TTL_SECONDS = 600


def _parse_enabled(raw: str | None, *, default: bool = False) -> bool:
    """Parse common truthy/falsey env values with a deterministic fallback."""
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if not normalized:
        return default
    return normalized not in {"0", "false", "off", "no"}


def context_cache_requested() -> bool:
    """Return whether explicit ADK context caching was requested by env."""
    return _parse_enabled(os.getenv(_CONTEXT_CACHE_ENABLED_ENV), default=False)


def _parse_int_env(raw: str | None, *, default: int, minimum: int, maximum: int | None = None) -> int:
    """Parse bounded integer env values with a deterministic fallback."""
    if raw is None:
        return default
    text = raw.strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def build_context_cache_config(*, profile: RunnerProfile) -> ContextCacheConfig | None:
    """Build ADK context-cache config for a runner profile.

    Context caching remains disabled by default and unavailable for ephemeral
    runners. Full-profile caching is opt-in because ADK caches system
    instruction, tools, tool config, and a request-history prefix.
    """
    if profile == "ephemeral":
        return None
    if profile != "full":
        raise ValueError(f"unsupported runner profile {profile!r}")
    if not context_cache_requested():
        return None

    return ContextCacheConfig(
        cache_intervals=_parse_int_env(
            os.getenv(_CONTEXT_CACHE_INTERVALS_ENV),
            default=_DEFAULT_CACHE_INTERVALS,
            minimum=1,
            maximum=100,
        ),
        min_tokens=_parse_int_env(
            os.getenv(_CONTEXT_CACHE_MIN_TOKENS_ENV),
            default=_DEFAULT_MIN_TOKENS,
            minimum=0,
        ),
        ttl_seconds=_parse_int_env(
            os.getenv(_CONTEXT_CACHE_TTL_SECONDS_ENV),
            default=_DEFAULT_TTL_SECONDS,
            minimum=1,
        ),
    )


__all__ = ["build_context_cache_config", "context_cache_requested"]
