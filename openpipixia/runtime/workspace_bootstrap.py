"""Workspace bootstrap injection for ADK model requests.

This module injects workspace bootstrap files into the model system prompt.
Supported files follow the openclaw-style order:
``AGENTS.md``, ``SOUL.md``, ``TOOLS.md``, ``IDENTITY.md``, ``USER.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_INJECTED_HEADER = "# Workspace Context (injected by openpipixia)"
_BOOTSTRAP_FILENAMES: tuple[str, ...] = (
    "AGENTS.md",
    "SOUL.md",
    "TOOLS.md",
    "IDENTITY.md",
    "USER.md",
)
_DEFAULT_MAX_CHARS_PER_FILE = 12000
_DEFAULT_MAX_TOTAL_CHARS = 30000


@dataclass(frozen=True, slots=True)
class BootstrapSection:
    """A single workspace bootstrap section prepared for prompt injection."""

    name: str
    content: str
    truncated: bool


def _workspace_root() -> Path:
    """Resolve workspace root from environment with a safe fallback."""
    raw = os.getenv("OPENPIPIXIA_WORKSPACE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.cwd()


def _parse_positive_int(raw: str | None, *, default: int) -> int:
    """Parse positive integer environment values with deterministic fallback."""
    if raw is None:
        return default
    text = raw.strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    return value if value > 0 else default


def _max_chars_per_file() -> int:
    """Return per-file character cap for injected bootstrap content."""
    return _parse_positive_int(
        os.getenv("OPENPIPIXIA_BOOTSTRAP_MAX_CHARS_PER_FILE"),
        default=_DEFAULT_MAX_CHARS_PER_FILE,
    )


def _max_total_chars() -> int:
    """Return total character cap across all injected bootstrap files."""
    return _parse_positive_int(
        os.getenv("OPENPIPIXIA_BOOTSTRAP_MAX_TOTAL_CHARS"),
        default=_DEFAULT_MAX_TOTAL_CHARS,
    )


def _truncate(text: str, *, limit: int) -> tuple[str, bool]:
    """Trim text to ``limit`` chars and report whether truncation happened."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def load_workspace_bootstrap_sections(workspace_root: Path | None = None) -> list[BootstrapSection]:
    """Load supported workspace files in fixed order for prompt injection.

    Args:
        workspace_root: Optional explicit workspace root. When omitted, uses
            ``OPENPIPIXIA_WORKSPACE`` or current working directory.

    Returns:
        Ordered sections loaded from existing files among
        ``AGENTS.md``, ``SOUL.md``, ``TOOLS.md``, ``IDENTITY.md``, ``USER.md``.
    """
    root = (workspace_root or _workspace_root()).expanduser().resolve(strict=False)
    per_file_limit = _max_chars_per_file()
    total_limit = _max_total_chars()
    used = 0
    sections: list[BootstrapSection] = []

    for filename in _BOOTSTRAP_FILENAMES:
        if used >= total_limit:
            break

        file_path = root / filename
        if not file_path.is_file():
            continue

        try:
            raw = file_path.read_text(encoding="utf-8")
        except Exception:
            continue

        clipped, truncated = _truncate(raw, limit=per_file_limit)
        remaining = total_limit - used
        clipped, total_truncated = _truncate(clipped, limit=remaining)
        truncated = truncated or total_truncated
        if not clipped:
            break

        sections.append(BootstrapSection(name=filename, content=clipped, truncated=truncated))
        used += len(clipped)

    return sections


def render_workspace_bootstrap_context(sections: list[BootstrapSection], workspace_root: Path) -> str:
    """Render loaded sections into a deterministic prompt block."""
    if not sections:
        return ""

    resolved_root = workspace_root.expanduser().resolve(strict=False)
    lines: list[str] = [
        _INJECTED_HEADER,
        f"Workspace: {resolved_root}",
        "",
        "The following workspace context files are loaded:",
        "",
    ]
    for section in sections:
        lines.extend([f"## {section.name}", "", section.content.rstrip()])
        if section.truncated:
            lines.append(f"[...truncated; read {section.name} for full content...]")
        lines.append("")
    return "\n".join(lines).strip()


async def before_model_workspace_bootstrap_callback(
    callback_context: Any,
    llm_request: Any,
) -> None:
    """Inject workspace bootstrap context into ``llm_request`` system instruction.

    This callback mutates ``llm_request.config.system_instruction`` in-place and
    keeps any existing system instruction after the injected workspace context.
    """
    # The callback context is currently unused for this minimal bootstrap
    # implementation, but we keep the canonical ADK parameter name to ensure
    # keyword-based callback invocation works.
    _ = callback_context

    config = getattr(llm_request, "config", None)
    if config is None:
        return None

    current = getattr(config, "system_instruction", None)
    if isinstance(current, str) and _INJECTED_HEADER in current:
        return None

    workspace_root = _workspace_root()
    sections = load_workspace_bootstrap_sections(workspace_root)
    if not sections:
        return None

    injected = render_workspace_bootstrap_context(sections, workspace_root)
    if not injected:
        return None

    if not current:
        config.system_instruction = injected
        return None
    if isinstance(current, str):
        config.system_instruction = f"{injected}\n\n{current}"
        return None
    if isinstance(current, list):
        config.system_instruction = [injected, *current]
        return None

    config.system_instruction = injected
    return None
