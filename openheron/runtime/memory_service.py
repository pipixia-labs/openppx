"""Memory service factory for ADK runner."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.adk.memory import InMemoryMemoryService

from ..core.config import get_data_dir
from .markdown_memory_service import MarkdownMemoryService


@dataclass(slots=True)
class MemoryConfig:
    """Runtime memory configuration for openheron.

    Attributes:
        enabled: Whether long-term memory is enabled for the runner.
        backend: Memory backend name. Supported values:
            - ``markdown`` (default)
            - ``in_memory`` (debug fallback)
        markdown_dir: Root directory for markdown memory files.
    """

    enabled: bool
    backend: str
    markdown_dir: str


def _parse_enabled(raw: str | None, *, default: bool) -> bool:
    """Parse common truthy/falsey env values with a deterministic fallback."""
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if not normalized:
        return default
    return normalized not in {"0", "false", "off", "no"}


def _default_markdown_dir() -> Path:
    """Resolve default markdown memory directory.

    By default memory files are colocated with workspace bootstrap files so the
    runtime consistently uses ``<workspace>/memory/{MEMORY.md,HISTORY.md}``.
    """
    workspace = os.getenv("OPENHERON_WORKSPACE", "").strip()
    if workspace:
        return Path(workspace).expanduser() / "memory"
    return get_data_dir() / "workspace" / "memory"


def load_memory_config() -> MemoryConfig:
    """Load memory configuration from environment variables.

    Environment variables:
        - ``OPENHERON_MEMORY_ENABLED`` (default: ``1``)
        - ``OPENHERON_MEMORY_BACKEND`` (default: ``markdown``)
        - ``OPENHERON_MEMORY_MARKDOWN_DIR`` (optional)
    """
    enabled = _parse_enabled(
        os.getenv("OPENHERON_MEMORY_ENABLED"),
        default=True,
    )
    backend = (
        os.getenv("OPENHERON_MEMORY_BACKEND", "markdown").strip().lower() or "markdown"
    )
    markdown_dir = os.getenv("OPENHERON_MEMORY_MARKDOWN_DIR", "").strip() or str(_default_markdown_dir())
    return MemoryConfig(
        enabled=enabled,
        backend=backend,
        markdown_dir=markdown_dir,
    )


def create_memory_service(config: MemoryConfig | None = None) -> Any | None:
    """Create an ADK memory service instance from runtime config.

    Fallback behavior is intentionally conservative:
    - If memory is disabled, returns ``None``.
    - Unknown backends fall back to in-memory to keep the agent runnable.
    """
    cfg = config or load_memory_config()
    if not cfg.enabled:
        return None

    if cfg.backend == "markdown":
        return MarkdownMemoryService(root_dir=cfg.markdown_dir)
    return InMemoryMemoryService()
