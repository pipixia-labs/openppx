"""Behavior tests for SQLite-backed memory service."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from openppx.runtime.sqlite_memory_service import SQLiteMemoryService


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


class SQLiteMemoryServiceTests(unittest.TestCase):
    def test_database_path_stamps_adk_meta_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agent" / "database" / "memory.db"
            SQLiteMemoryService(db_path=db_path)

            payload = json.loads((db_path.parent / ".adk_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["adk_major"], 2)
            self.assertEqual(payload["last_writer"], "openppx")

    def test_add_events_and_search_fact_memory(self) -> None:
        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "memory.db"
                service = SQLiteMemoryService(db_path=db_path)
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
                    app_name="openppx",
                    user_id="human:local:user-1",
                    session_id="s1",
                    events=events,
                )

                response = await service.search_memory(
                    app_name="openppx",
                    user_id="human:local:user-1",
                    query="alpha",
                )

                self.assertEqual(len(response.memories), 1)
                memory = response.memories[0]
                self.assertEqual(memory.custom_metadata["source"], "fact")
                self.assertEqual(memory.custom_metadata["category"], "context")
                self.assertIn("Alpha", memory.content.parts[0].text or "")
                self.assertEqual(
                    memory.timestamp,
                    datetime.fromtimestamp(1710000000.0, tz=timezone.utc).isoformat(),
                )

                with sqlite3.connect(db_path) as conn:
                    fact_count = conn.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0]
                    archive_count = conn.execute("SELECT COUNT(*) FROM memory_archive_index").fetchone()[0]

                self.assertEqual(fact_count, 1)
                self.assertEqual(archive_count, 2)

        asyncio.run(_scenario())

    def test_search_can_fall_back_to_archive_text(self) -> None:
        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                service = SQLiteMemoryService(db_path=Path(tmp) / "memory.db")
                await service.add_events_to_memory(
                    app_name="openppx",
                    user_id="human:local:user-2",
                    session_id="s1",
                    events=[
                        _Event(
                            id="e-archive",
                            author="agent",
                            content=_Content(parts=[_Part(text="Temporary note about zebra migration")]),
                            timestamp=1710000010.0,
                        )
                    ],
                )

                response = await service.search_memory(
                    app_name="openppx",
                    user_id="human:local:user-2",
                    query="zebra",
                )

                self.assertEqual(len(response.memories), 1)
                self.assertEqual(response.memories[0].custom_metadata["source"], "archive")
                self.assertIn("zebra", (response.memories[0].content.parts[0].text or "").lower())

        asyncio.run(_scenario())

    def test_add_session_deduplicates_archive_and_fact_rows(self) -> None:
        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "memory.db"
                service = SQLiteMemoryService(db_path=db_path)
                session = _Session(
                    app_name="openppx",
                    user_id="human:local:user-3",
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
                    app_name="openppx",
                    user_id="human:local:user-3",
                    query="concise",
                )
                self.assertEqual(len(response.memories), 1)

                with sqlite3.connect(db_path) as conn:
                    archive_count = conn.execute("SELECT COUNT(*) FROM memory_archive_index").fetchone()[0]
                    fact_count = conn.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0]

                self.assertEqual(archive_count, 1)
                self.assertEqual(fact_count, 1)

        asyncio.run(_scenario())

    def test_add_memory_is_searchable(self) -> None:
        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                service = SQLiteMemoryService(db_path=Path(tmp) / "memory.db")
                await service.add_memory(
                    app_name="openppx",
                    user_id="human:local:user-4",
                    memories=[
                        MemoryEntry(
                            author="memory",
                            timestamp="2026-04-18T10:00:00+00:00",
                            content=types.Content(
                                role="user",
                                parts=[types.Part(text="Lives in Seattle")],
                            ),
                            custom_metadata={"category": "context"},
                        ),
                        MemoryEntry(
                            author="memory",
                            timestamp="2026-04-18T10:01:00+00:00",
                            content=types.Content(
                                role="user",
                                parts=[types.Part(text="Prefers morning meetings")],
                            ),
                            custom_metadata={"category": "preferences"},
                        ),
                    ],
                )

                response = await service.search_memory(
                    app_name="openppx",
                    user_id="human:local:user-4",
                    query="Seattle",
                )

                self.assertEqual(len(response.memories), 1)
                self.assertEqual(response.memories[0].custom_metadata["source"], "fact")
                self.assertIn("Seattle", response.memories[0].content.parts[0].text or "")

        asyncio.run(_scenario())


if __name__ == "__main__":
    unittest.main()
