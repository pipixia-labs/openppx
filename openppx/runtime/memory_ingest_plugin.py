"""ADK plugin for writing completed invocation events to memory."""

from __future__ import annotations

from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins.base_plugin import BasePlugin

from .interaction_context import (
    INTERACTION_CONTEXT_STATE_KEY,
    MEMORY_INGEST_OFFSET_STATE_KEY,
)


def _agent_name(agent: Any) -> str:
    name = getattr(agent, "name", None)
    return name if isinstance(name, str) else ""


class OpenPpxMemoryIngestPlugin(BasePlugin):
    """Persist newly produced ADK events into configured memory service."""

    def __init__(self, *, target_agent_name: str | None = None) -> None:
        super().__init__(name="openppx_memory_ingest")
        self._target_agent_name = target_agent_name

    def _matches_agent(self, agent: Any) -> bool:
        if not self._target_agent_name:
            return True
        return _agent_name(agent) == self._target_agent_name

    async def before_agent_callback(
        self,
        *,
        agent: Any,
        callback_context: CallbackContext,
    ) -> None:
        """Store an ingest offset fallback when gateway did not provide one."""
        if not self._matches_agent(agent):
            return None

        state = getattr(callback_context, "state", None)
        session = getattr(callback_context, "session", None)
        if state is None or session is None:
            return None
        if state.get(MEMORY_INGEST_OFFSET_STATE_KEY) is not None:
            return None
        state[MEMORY_INGEST_OFFSET_STATE_KEY] = len(getattr(session, "events", []) or [])
        return None

    async def after_agent_callback(
        self,
        *,
        agent: Any,
        callback_context: CallbackContext,
    ) -> None:
        """Persist events appended since the stored ingest offset."""
        if not self._matches_agent(agent):
            return None

        state = getattr(callback_context, "state", None)
        session = getattr(callback_context, "session", None)
        if state is None or session is None:
            return None

        interaction_context = state.get(INTERACTION_CONTEXT_STATE_KEY) or {}
        if isinstance(interaction_context, dict) and not interaction_context.get("memory_ingest_enabled", True):
            return None

        raw_offset = state.get(MEMORY_INGEST_OFFSET_STATE_KEY, 0)
        try:
            offset = max(0, int(raw_offset))
        except (TypeError, ValueError):
            offset = 0
        events = list(getattr(session, "events", []) or [])
        delta_events = events[offset:]
        if not delta_events:
            return None

        custom_metadata: dict[str, Any] = {"ingest_reason": "after_agent_callback"}
        if isinstance(interaction_context, dict):
            custom_metadata.update(interaction_context)
        try:
            await callback_context.add_events_to_memory(
                events=delta_events,
                custom_metadata=custom_metadata,
            )
        except ValueError:
            return None
        return None
