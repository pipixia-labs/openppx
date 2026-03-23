"""Behavior tests for markdown-backed memory service."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openpipixia.runtime.markdown_memory_service import MarkdownMemoryService


@dataclass(slots=True)
class _Part:
    text: str


@dataclass(slots=True)
class _Content:
    parts: list[_Part]


@dataclass(slots=True)
class _Event:
    id: str
    author: str
    content: _Content
    timestamp: float | None = None


@dataclass(slots=True)
class _Session:
    app_name: str
    user_id: str
    id: str
    events: list[_Event]


class MarkdownMemoryServiceTests(unittest.TestCase):
    def test_add_events_and_search(self) -> None:
        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                service = MarkdownMemoryService(root_dir=tmp)
                events = [
                    _Event(
                        id="e1",
                        author="user",
                        content=_Content(parts=[_Part(text="My project is Alpha")]),
                        timestamp=1710000000.0,
                    ),
                    _Event(
                        id="e2",
                        author="agent",
                        content=_Content(parts=[_Part(text="Noted project details")]),
                        timestamp=1710000001.0,
                    ),
                ]
                await service.add_events_to_memory(
                    app_name="openpipixia",
                    user_id="user-1",
                    session_id="s1",
                    events=events,
                )
                response = await service.search_memory(
                    app_name="openpipixia",
                    user_id="user-1",
                    query="alpha",
                )
                self.assertEqual(len(response.memories), 1)
                text = response.memories[0].content.parts[0].text or ""
                self.assertIn("Alpha", text)

                memory_text = (Path(tmp) / "MEMORY.md").read_text(encoding="utf-8")
                history_text = (Path(tmp) / "HISTORY.md").read_text(encoding="utf-8")
                expected_ts = datetime.fromtimestamp(1710000000.0, tz=timezone.utc).isoformat()

                self.assertIn("My project is Alpha", memory_text)
                self.assertIn("[category=context]", memory_text)
                self.assertIn(expected_ts, memory_text)
                self.assertIn("My project is Alpha", history_text)
                self.assertIn("Noted project details", history_text)

        asyncio.run(_scenario())

    def test_search_is_user_scoped(self) -> None:
        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                service = MarkdownMemoryService(root_dir=tmp)
                await service.add_memory(
                    app_name="openpipixia",
                    user_id="alice",
                    memories=["Alice likes green tea"],
                )
                await service.add_memory(
                    app_name="openpipixia",
                    user_id="bob",
                    memories=["Bob likes black coffee"],
                )
                response = await service.search_memory(
                    app_name="openpipixia",
                    user_id="bob",
                    query="green tea",
                )
                self.assertEqual(len(response.memories), 0)

        asyncio.run(_scenario())

    def test_add_session_deduplicates_event_ids(self) -> None:
        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                service = MarkdownMemoryService(root_dir=tmp)
                session = _Session(
                    app_name="openpipixia",
                    user_id="user-2",
                    id="session-1",
                    events=[
                        _Event(
                            id="event-42",
                            author="user",
                            content=_Content(parts=[_Part(text="I prefer concise updates")]),
                        )
                    ],
                )
                await service.add_session_to_memory(session)
                await service.add_session_to_memory(session)
                response = await service.search_memory(
                    app_name="openpipixia",
                    user_id="user-2",
                    query="concise",
                )
                self.assertEqual(len(response.memories), 1)
                history_text = (Path(tmp) / "HISTORY.md").read_text(encoding="utf-8")
                self.assertEqual(history_text.count("I prefer concise updates"), 1)

        asyncio.run(_scenario())

    def test_add_memory_is_searchable(self) -> None:
        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                service = MarkdownMemoryService(root_dir=tmp)
                await service.add_memory(
                    app_name="openpipixia",
                    user_id="user-3",
                    memories=["Lives in Seattle", "Prefers morning meetings"],
                )
                response = await service.search_memory(
                    app_name="openpipixia",
                    user_id="user-3",
                    query="Seattle",
                )
                self.assertEqual(len(response.memories), 1)
                self.assertIn("Seattle", response.memories[0].content.parts[0].text or "")

        asyncio.run(_scenario())


if __name__ == "__main__":
    unittest.main()
