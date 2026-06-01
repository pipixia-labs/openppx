"""Agent bootstrap injection for ADK model requests.

This module injects per-agent bootstrap files into the model request contents.
Supported files follow the openclaw-style order:
``AGENTS.md``, ``SOUL.md``, ``TOOLS.md``, ``IDENTITY.md``, ``USER.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from ..core.config import get_agent_home_dir

_INJECTED_HEADER = "# Agent Context (injected by openppx)"
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
    """Resolve the active per-agent config root with a safe fallback."""
    return get_agent_home_dir()


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
        os.getenv("OPENPPX_BOOTSTRAP_MAX_CHARS_PER_FILE"),
        default=_DEFAULT_MAX_CHARS_PER_FILE,
    )


def _max_total_chars() -> int:
    """Return total character cap across all injected bootstrap files."""
    return _parse_positive_int(
        os.getenv("OPENPPX_BOOTSTRAP_MAX_TOTAL_CHARS"),
        default=_DEFAULT_MAX_TOTAL_CHARS,
    )


def _truncate(text: str, *, limit: int) -> tuple[str, bool]:
    """Trim text to ``limit`` chars and report whether truncation happened."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def load_workspace_bootstrap_sections(workspace_root: Path | None = None) -> list[BootstrapSection]:
    """Load supported agent-home files in fixed order for prompt injection.

    Args:
        workspace_root: Optional explicit agent-home root. When omitted, uses
            the active per-agent config directory.

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
        f"Agent home: {resolved_root}",
        "",
        "The following agent context files are loaded:",
        "",
    ]
    for section in sections:
        lines.extend([f"## {section.name}", "", section.content.rstrip()])
        if section.truncated:
            lines.append(f"[...truncated; read {section.name} for full content...]")
        lines.append("")
    return "\n".join(lines).strip()


def _has_injected_context(system_instruction: Any) -> bool:
    """Return whether legacy system instruction already includes context."""
    if isinstance(system_instruction, str):
        return _INJECTED_HEADER in system_instruction
    if isinstance(system_instruction, list):
        return any(isinstance(item, str) and _INJECTED_HEADER in item for item in system_instruction)
    return False


def _content_text(content: Any) -> str:
    """Return concatenated text parts from a GenAI content-like object."""
    parts = getattr(content, "parts", None) or []
    text_parts: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str):
            text_parts.append(text)
    return "\n".join(text_parts)


def _contents_have_injected_context(contents: Any) -> bool:
    """Return whether request contents already include bootstrap context."""
    if not isinstance(contents, list):
        return False
    return any(_INJECTED_HEADER in _content_text(content) for content in contents)


def _content_contains_function_response(content: Any) -> bool:
    """Return whether content contains tool/function response parts."""
    parts = getattr(content, "parts", None) or []
    return any(getattr(part, "function_response", None) is not None for part in parts)


def _insert_before_latest_user_batch(contents: list[Any], content: Any) -> None:
    """Insert content into the latest user batch so ADK cache will not cache it."""
    insert_index = len(contents)
    if contents:
        for index in range(len(contents) - 1, -1, -1):
            existing = contents[index]
            if getattr(existing, "role", None) != "user":
                insert_index = index + 1
                break
            if _content_contains_function_response(existing):
                insert_index = index + 1
                break
            insert_index = index
    contents[insert_index:insert_index] = [content]


async def before_model_workspace_bootstrap_callback(
    callback_context: Any,
    llm_request: Any,
) -> None:
    """Inject agent bootstrap context into ``llm_request`` contents.

    Dynamic agent-home context must not enter ``system_instruction`` because ADK
    explicit context caching includes system instruction in the cache payload.
    The bootstrap block is inserted into the latest user-content batch so the
    Gemini cache prefix algorithm treats it as non-cached request content.
    """
    # The callback context is currently unused for this minimal bootstrap
    # implementation, but we keep the canonical ADK parameter name to ensure
    # keyword-based callback invocation works.
    _ = callback_context

    config = getattr(llm_request, "config", None)
    current = getattr(config, "system_instruction", None)
    contents = getattr(llm_request, "contents", None)
    if _has_injected_context(current) or _contents_have_injected_context(contents):
        return None

    workspace_root = _workspace_root()
    sections = load_workspace_bootstrap_sections(workspace_root)
    if not sections:
        return None

    injected = render_workspace_bootstrap_context(sections, workspace_root)
    if not injected:
        return None

    if not isinstance(contents, list):
        contents = []
        llm_request.contents = contents

    bootstrap_content = types.Content(
        role="user",
        parts=[types.Part.from_text(text=injected)],
    )
    _insert_before_latest_user_batch(contents, bootstrap_content)
    return None


def _callback_agent_name(callback_context: Any) -> str:
    name = getattr(callback_context, "agent_name", None)
    return name if isinstance(name, str) else ""


class OpenPpxWorkspaceBootstrapPlugin(BasePlugin):
    """Inject agent-home bootstrap files before model requests."""

    def __init__(self, *, target_agent_name: str | None = None) -> None:
        super().__init__(name="openppx_workspace_bootstrap")
        self._target_agent_name = target_agent_name

    def _matches_agent(self, callback_context: Any) -> bool:
        if not self._target_agent_name:
            return True
        agent_name = _callback_agent_name(callback_context)
        return not agent_name or agent_name == self._target_agent_name

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> None:
        if not self._matches_agent(callback_context):
            return None
        await before_model_workspace_bootstrap_callback(callback_context, llm_request)
        return None
