"""SQLite-backed long-term memory service for openppx."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import hashlib
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import asdict
from dataclasses import is_dataclass
from pathlib import Path
from typing import Any

from google.adk.memory.base_memory_service import BaseMemoryService, SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from .adk_storage_meta import ensure_adk_storage_meta_for_sqlite_path
from .memory_shared import (
    build_fact_key,
    content_text_for_memory,
    event_text_for_history,
    event_text_for_memory,
    event_timestamp_iso,
    infer_fact_category,
    is_user_author,
    memory_entry_text,
    now_iso,
    tokenize,
)

_UNKNOWN_SESSION_ID = "__unknown_session_id__"
_SEARCH_LIMIT = 12


def _now_ms() -> int:
    """Return current wall-clock milliseconds."""
    return int(time.time() * 1000)


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open one SQLite connection with pragmatic defaults."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _workspace_fallback_db_path(db_path: Path) -> Path:
    """Return the workspace-local fallback path for one SQLite database."""
    fallback = (Path.cwd() / ".openppx" / "database" / db_path.name).resolve(strict=False)
    fallback.parent.mkdir(parents=True, exist_ok=True)
    return fallback


def _prepare_db_path(db_path: Path) -> Path:
    """Return a writable SQLite path, falling back to workspace-local storage."""
    candidate = db_path.expanduser().resolve(strict=False)
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    except PermissionError:
        return _workspace_fallback_db_path(candidate)


def _json_dumps(payload: Any) -> str:
    """Serialize one JSON payload with permissive fallback behavior."""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _json_loads(raw: str | None, *, default: Any) -> Any:
    """Deserialize JSON text with a caller-provided fallback."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _serialize_content(content: object) -> str:
    """Serialize one content-like object to JSON when possible."""
    if content is None:
        return ""
    payload: Any
    if hasattr(content, "model_dump"):
        payload = content.model_dump(mode="json")
    elif is_dataclass(content):
        payload = asdict(content)
    elif isinstance(content, Mapping):
        payload = dict(content)
    else:
        return ""
    return _json_dumps(payload)


def _fallback_content(*, text: str, author: str | None) -> types.Content:
    """Build a plain text content object when structured content is unavailable."""
    role = "user" if is_user_author(author or "") else "model"
    return types.Content(role=role, parts=[types.Part(text=text)])


def _deserialize_content(*, raw_json: str, fallback_text: str, author: str | None) -> types.Content:
    """Deserialize stored content JSON or fall back to plain text content."""
    payload = _json_loads(raw_json, default=None)
    if isinstance(payload, dict):
        try:
            return types.Content.model_validate(payload)
        except Exception:
            pass
    return _fallback_content(text=fallback_text, author=author)


def _event_key(
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    event_id: str,
    author: str,
    timestamp: str,
    text: str,
) -> str:
    """Build a stable event archive key for dedupe."""
    raw = "\n".join([app_name, user_id, session_id, event_id, author, timestamp, text])
    if event_id:
        return f"{app_name}:{user_id}:{session_id}:{event_id}"
    return f"archive:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:40]}"


class SQLiteMemoryService(BaseMemoryService):
    """SQLite-backed ADK memory service with facts plus raw archive index."""

    def __init__(self, *, db_path: str | Path):
        self._db_path = _prepare_db_path(Path(db_path))
        ensure_adk_storage_meta_for_sqlite_path(self._db_path)
        self._lock = threading.Lock()
        try:
            self._ensure_schema()
        except sqlite3.OperationalError:
            fallback = _workspace_fallback_db_path(self._db_path)
            if fallback == self._db_path:
                raise
            self._db_path = fallback
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create SQLite tables and indexes when missing."""
        with _connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_facts (
                    id TEXT PRIMARY KEY,
                    app_name TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    fact_key TEXT NOT NULL,
                    author TEXT,
                    category TEXT NOT NULL,
                    text TEXT NOT NULL,
                    timestamp TEXT,
                    custom_metadata_json TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    UNIQUE(app_name, user_id, fact_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_archive_index (
                    id TEXT PRIMARY KEY,
                    app_name TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    event_id TEXT,
                    author TEXT,
                    text TEXT NOT NULL,
                    timestamp TEXT,
                    content_json TEXT,
                    custom_metadata_json TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_facts_scope "
                "ON memory_facts(app_name, user_id, updated_at_ms DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_archive_scope "
                "ON memory_archive_index(app_name, user_id, created_at_ms DESC)"
            )

    async def add_session_to_memory(self, session: object) -> None:
        """Ingest all events from one session."""
        await self.add_events_to_memory(
            app_name=getattr(session, "app_name", ""),
            user_id=getattr(session, "user_id", ""),
            session_id=getattr(session, "id", None),
            events=getattr(session, "events", []),
        )

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[object],
        session_id: str | None = None,
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Ingest incremental event deltas into facts and raw archive tables."""
        if not app_name or not user_id or not events:
            return

        scoped_session_id = (session_id or _UNKNOWN_SESSION_ID).strip() or _UNKNOWN_SESSION_ID
        metadata_json = _json_dumps(dict(custom_metadata or {}))
        now_ms = _now_ms()

        with self._lock, _connect(self._db_path) as conn:
            for event in events:
                archive_text = event_text_for_history(event)
                fact_text = event_text_for_memory(event)
                if not archive_text and not fact_text:
                    continue

                timestamp = event_timestamp_iso(event)
                author = str(getattr(event, "author", "") or "").strip() or "unknown"
                event_id = str(getattr(event, "id", "") or "").strip()
                event_key = _event_key(
                    app_name=app_name,
                    user_id=user_id,
                    session_id=scoped_session_id,
                    event_id=event_id,
                    author=author,
                    timestamp=timestamp,
                    text=archive_text or fact_text,
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_archive_index (
                        id,
                        app_name,
                        user_id,
                        session_id,
                        event_id,
                        author,
                        text,
                        timestamp,
                        content_json,
                        custom_metadata_json,
                        created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_key,
                        app_name,
                        user_id,
                        scoped_session_id,
                        event_id or None,
                        author,
                        archive_text or fact_text,
                        timestamp,
                        _serialize_content(getattr(event, "content", None)),
                        metadata_json,
                        now_ms,
                    ),
                )

                if not fact_text or not is_user_author(author):
                    continue
                category = infer_fact_category(fact_text)
                if not category:
                    continue
                fact_key = build_fact_key(category=category, text=fact_text)
                fact_id = f"fact:{fact_key}"
                conn.execute(
                    """
                    INSERT INTO memory_facts (
                        id,
                        app_name,
                        user_id,
                        session_id,
                        fact_key,
                        author,
                        category,
                        text,
                        timestamp,
                        custom_metadata_json,
                        created_at_ms,
                        updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(app_name, user_id, fact_key) DO UPDATE SET
                        session_id=excluded.session_id,
                        author=excluded.author,
                        text=excluded.text,
                        timestamp=excluded.timestamp,
                        custom_metadata_json=excluded.custom_metadata_json,
                        updated_at_ms=excluded.updated_at_ms
                    """,
                    (
                        fact_id,
                        app_name,
                        user_id,
                        scoped_session_id,
                        fact_key,
                        author,
                        category,
                        fact_text,
                        timestamp,
                        metadata_json,
                        now_ms,
                        now_ms,
                    ),
                )

    async def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Persist explicit memory entries directly into the facts table."""
        if not app_name or not user_id or not memories:
            return

        write_metadata = dict(custom_metadata or {})
        base_session_id = str(write_metadata.get("session_id", _UNKNOWN_SESSION_ID) or _UNKNOWN_SESSION_ID)
        base_timestamp = str(write_metadata.get("dialogue_timestamp", "") or "").strip()
        now_ms = _now_ms()

        with self._lock, _connect(self._db_path) as conn:
            for memory in memories:
                text = memory_entry_text(memory)
                if not text:
                    continue
                if isinstance(memory, MemoryEntry):
                    merged_metadata = dict(write_metadata)
                    merged_metadata.update(memory.custom_metadata or {})
                    author = str(memory.author or "memory")
                    timestamp = str(memory.timestamp or base_timestamp or now_iso())
                    content_json = _serialize_content(memory.content)
                else:
                    merged_metadata = dict(write_metadata)
                    author = "memory"
                    timestamp = base_timestamp or now_iso()
                    content_json = ""
                category = str(merged_metadata.get("category") or infer_fact_category(text) or "context")
                fact_key = build_fact_key(category=category, text=text)
                fact_id = f"fact:{fact_key}"
                session_id = str(merged_metadata.get("session_id") or base_session_id)
                metadata_json = _json_dumps(merged_metadata)
                conn.execute(
                    """
                    INSERT INTO memory_facts (
                        id,
                        app_name,
                        user_id,
                        session_id,
                        fact_key,
                        author,
                        category,
                        text,
                        timestamp,
                        custom_metadata_json,
                        created_at_ms,
                        updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(app_name, user_id, fact_key) DO UPDATE SET
                        session_id=excluded.session_id,
                        author=excluded.author,
                        text=excluded.text,
                        timestamp=excluded.timestamp,
                        custom_metadata_json=excluded.custom_metadata_json,
                        updated_at_ms=excluded.updated_at_ms
                    """,
                    (
                        fact_id,
                        app_name,
                        user_id,
                        session_id,
                        fact_key,
                        author,
                        category,
                        text,
                        timestamp,
                        metadata_json,
                        now_ms,
                        now_ms,
                    ),
                )
                if content_json:
                    archive_id = f"memory:{fact_key}"
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO memory_archive_index (
                            id,
                            app_name,
                            user_id,
                            session_id,
                            event_id,
                            author,
                            text,
                            timestamp,
                            content_json,
                            custom_metadata_json,
                            created_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            archive_id,
                            app_name,
                            user_id,
                            session_id,
                            fact_id,
                            author,
                            text,
                            timestamp,
                            content_json,
                            metadata_json,
                            now_ms,
                        ),
                    )

    async def search_memory(self, *, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        """Search facts first, then fall back to raw archive text for self recall."""
        response = SearchMemoryResponse()
        query_tokens = tokenize(query)
        if not app_name or not user_id or not query_tokens:
            return response

        seen_ids: set[str] = set()
        with self._lock, _connect(self._db_path) as conn:
            fact_rows = conn.execute(
                """
                SELECT id, author, category, text, timestamp, custom_metadata_json
                FROM memory_facts
                WHERE app_name = ? AND user_id = ?
                ORDER BY updated_at_ms DESC
                LIMIT 200
                """,
                (app_name, user_id),
            ).fetchall()
            for row in fact_rows:
                text = str(row["text"] or "")
                if query_tokens.isdisjoint(tokenize(text)):
                    continue
                memory_id = str(row["id"])
                if memory_id in seen_ids:
                    continue
                metadata = _json_loads(row["custom_metadata_json"], default={})
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata.setdefault("source", "fact")
                metadata.setdefault("category", row["category"])
                response.memories.append(
                    MemoryEntry(
                        id=memory_id,
                        author=row["author"],
                        timestamp=row["timestamp"],
                        custom_metadata=metadata,
                        content=_fallback_content(text=text, author=row["author"]),
                    )
                )
                seen_ids.add(memory_id)
                if len(response.memories) >= _SEARCH_LIMIT:
                    return response

            if response.memories:
                return response

            archive_rows = conn.execute(
                """
                SELECT id, author, text, timestamp, content_json, custom_metadata_json
                FROM memory_archive_index
                WHERE app_name = ? AND user_id = ?
                ORDER BY created_at_ms DESC
                LIMIT 300
                """,
                (app_name, user_id),
            ).fetchall()
            for row in archive_rows:
                text = str(row["text"] or "")
                if query_tokens.isdisjoint(tokenize(text)):
                    continue
                memory_id = str(row["id"])
                if memory_id in seen_ids:
                    continue
                metadata = _json_loads(row["custom_metadata_json"], default={})
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata.setdefault("source", "archive")
                response.memories.append(
                    MemoryEntry(
                        id=memory_id,
                        author=row["author"],
                        timestamp=row["timestamp"],
                        custom_metadata=metadata,
                        content=_deserialize_content(
                            raw_json=str(row["content_json"] or ""),
                            fallback_text=text,
                            author=row["author"],
                        ),
                    )
                )
                seen_ids.add(memory_id)
                if len(response.memories) >= _SEARCH_LIMIT:
                    break

        return response
