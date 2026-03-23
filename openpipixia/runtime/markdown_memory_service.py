"""Markdown-backed ADK memory service.

This service persists long-term facts and raw text history into two markdown
files under one memory root directory:

- ``MEMORY.md`` keeps extracted long-term facts for retrieval.
- ``HISTORY.md`` keeps append-only raw conversation transcript (text only).
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from google.adk.memory.base_memory_service import BaseMemoryService, SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

_MEMORY_FILE = "MEMORY.md"
_HISTORY_FILE = "HISTORY.md"
_MEMORY_HEADER = """# Long-term Memory

This file stores extracted long-term facts from conversations.
Each entry is timestamped with the original dialogue time.
"""
_HISTORY_HEADER = """# Conversation History

Append-only raw text transcript of conversations.
Do not rewrite existing content.
"""
_FACT_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "preferences",
        (
            "i prefer",
            "i like",
            "i don't like",
            "i dislike",
            "prefer to",
            "preference",
            "我喜欢",
            "我不喜欢",
            "偏好",
            "习惯",
        ),
    ),
    (
        "relationships",
        (
            "my wife",
            "my husband",
            "my girlfriend",
            "my boyfriend",
            "my friend",
            "my manager",
            "my team",
            "colleague",
            "关系",
            "朋友",
            "同事",
            "家人",
            "团队",
        ),
    ),
    (
        "context",
        (
            "my project",
            "our project",
            "working on",
            "deadline",
            "background",
            "my name is",
            "i am",
            "i'm",
            "我叫",
            "我是",
            "项目",
            "背景",
            "目标",
            "计划",
            "仓库",
        ),
    ),
)


def _sanitize_scope(value: str) -> str:
    """Sanitize scope keys for filesystem-safe paths."""
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return sanitized or "_"


def _tokenize(text: str) -> set[str]:
    """Tokenize text for lightweight keyword matching."""
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]+", text)}


def _event_text_lines(event: object) -> list[str]:
    """Extract text parts from an ADK event-like object in original order."""
    content = getattr(event, "content", None)
    if not content or not getattr(content, "parts", None):
        return []

    lines: list[str] = []
    for part in content.parts:
        text = getattr(part, "text", None)
        if not isinstance(text, str):
            continue
        if not text:
            continue
        lines.append(text)
    return lines


def _event_text_for_memory(event: object) -> str:
    """Return a normalized text string used for memory fact extraction."""
    lines = _event_text_lines(event)
    if not lines:
        return ""
    return " ".join(segment.strip() for segment in lines if segment.strip()).strip()


def _event_text_for_history(event: object) -> str:
    """Return raw text transcript representation used in HISTORY.md."""
    lines = _event_text_lines(event)
    if not lines:
        return ""
    return "\n".join(lines)


def _iso_from_unix_seconds(raw: object) -> str | None:
    """Convert unix timestamp seconds to ISO8601 string when possible."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _now_iso() -> str:
    """Return current UTC time in ISO8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _event_timestamp_iso(event: object) -> str:
    """Resolve event timestamp, falling back to current time when missing."""
    return _iso_from_unix_seconds(getattr(event, "timestamp", None)) or _now_iso()


def _is_user_author(author: str) -> bool:
    """Best-effort check whether an event author represents the user side."""
    normalized = (author or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"user", "human"}:
        return True
    if normalized.endswith("user"):
        return True
    return False


def _infer_fact_category(text: str) -> str | None:
    """Infer long-term fact category from one text snippet.

    Returns one of ``preferences``, ``context``, ``relationships``, or ``None``
    when the text does not look like a durable fact.
    """
    lowered = text.lower()
    for category, keywords in _FACT_CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return category
    return None


class MarkdownMemoryService(BaseMemoryService):
    """Markdown-backed local memory service.

    Storage layout under ``<root>``:
    - ``MEMORY.md``: extracted long-term facts for retrieval
    - ``HISTORY.md``: append-only raw conversation transcript
    - ``.event_ids.<app>.<user>.json``: ingested event ids for deduplication
    """

    def __init__(self, *, root_dir: str | Path):
        self._root_dir = Path(root_dir).expanduser().resolve()
        self._lock = threading.Lock()

    def _memory_file(self) -> Path:
        return self._root_dir / _MEMORY_FILE

    def _history_file(self) -> Path:
        return self._root_dir / _HISTORY_FILE

    def _event_ids_file(self, *, app_name: str, user_id: str) -> Path:
        app_key = _sanitize_scope(app_name)
        user_key = _sanitize_scope(user_id)
        return self._root_dir / f".event_ids.{app_key}.{user_key}.json"

    def _ensure_root(self) -> None:
        self._root_dir.mkdir(parents=True, exist_ok=True)

    def _ensure_markdown_file(self, path: Path, *, header: str) -> None:
        """Create markdown file once with header, never rewriting existing file."""
        if path.exists():
            return
        path.write_text(header.rstrip() + "\n\n", encoding="utf-8")

    def _load_event_ids(self, *, app_name: str, user_id: str) -> set[str]:
        event_ids_path = self._event_ids_file(app_name=app_name, user_id=user_id)
        if not event_ids_path.exists():
            return set()
        try:
            raw = json.loads(event_ids_path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        if not isinstance(raw, list):
            return set()
        return {str(item) for item in raw if isinstance(item, str) and item}

    def _save_event_ids(self, *, app_name: str, user_id: str, event_ids: set[str]) -> None:
        event_ids_path = self._event_ids_file(app_name=app_name, user_id=user_id)
        payload = sorted(event_ids)
        event_ids_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _append_history_entries(self, *, entries: Sequence[dict[str, str]]) -> None:
        """Append text transcript entries to HISTORY.md without modifying old lines."""
        if not entries:
            return
        history_path = self._history_file()
        self._ensure_markdown_file(history_path, header=_HISTORY_HEADER)

        lines: list[str] = []
        for entry in entries:
            lines.extend(
                [
                    (
                        "## "
                        f"{entry['timestamp']} | "
                        f"app={entry['app_name']} | "
                        f"user={entry['user_id']} | "
                        f"session={entry['session_id']} | "
                        f"author={entry['author']} | "
                        f"event={entry['event_id']}"
                    ),
                    entry["text"],
                    "",
                ]
            )

        with history_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _append_memory_entries(self, *, entries: Sequence[dict[str, str]]) -> None:
        """Append extracted long-term facts into MEMORY.md."""
        if not entries:
            return
        memory_path = self._memory_file()
        self._ensure_markdown_file(memory_path, header=_MEMORY_HEADER)

        lines: list[str] = []
        for entry in entries:
            lines.append(
                (
                    f"- [{entry['timestamp']}] "
                    f"[app={entry['app_name']}] "
                    f"[user={entry['user_id']}] "
                    f"[session={entry['session_id']}] "
                    f"[author={entry['author']}] "
                    f"[category={entry['category']}] "
                    f"{entry['text']}"
                )
            )
        lines.append("")

        with memory_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _parse_memory_line(self, line: str) -> tuple[str, str, str | None, str | None, str] | None:
        """Parse one memory line into ``(app_name, user_id, author, timestamp, text)``."""
        pattern = (
            r"^- \[(?P<timestamp>[^\]]+)\]\s+"
            r"\[app=(?P<app>[^\]]+)\]\s+"
            r"\[user=(?P<user>[^\]]+)\]\s+"
            r"\[session=(?P<session>[^\]]*)\]\s+"
            r"\[author=(?P<author>[^\]]*)\]\s+"
            r"\[category=(?P<category>[^\]]+)\]\s*(?P<text>.*)$"
        )
        match = re.match(pattern, line)
        if match:
            app_name = match.group("app").strip()
            user_id = match.group("user").strip()
            author = match.group("author").strip() or None
            timestamp = match.group("timestamp").strip() or None
            text = match.group("text").strip()
            if not app_name or not user_id or not text:
                return None
            return app_name, user_id, author, timestamp, text

        # Compatibility path for historical lines: "- [author] text".
        legacy = re.match(r"^- \[(?P<author>[^\]]+)\]\s*(?P<text>.*)$", line)
        if legacy:
            text = legacy.group("text").strip()
            author = legacy.group("author").strip() or None
            if not text:
                return None
            return "", "", author, None, text
        return None

    async def add_session_to_memory(self, session: object) -> None:
        """Ingest all events in a session with event-id deduplication."""
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
        """Ingest event deltas into history and long-term memory markdown files."""
        _ = custom_metadata
        if not app_name or not user_id:
            return
        if not events:
            return

        with self._lock:
            self._ensure_root()
            known_event_ids = self._load_event_ids(app_name=app_name, user_id=user_id)
            new_event_ids: set[str] = set()
            history_entries: list[dict[str, str]] = []
            memory_entries: list[dict[str, str]] = []

            for event in events:
                event_id = str(getattr(event, "id", "") or "").strip()
                if event_id and event_id in known_event_ids:
                    continue

                history_text = _event_text_for_history(event)
                memory_text = _event_text_for_memory(event)
                if not history_text and not memory_text:
                    continue

                timestamp = _event_timestamp_iso(event)
                author = str(getattr(event, "author", "") or "").strip() or "unknown"
                session_label = (session_id or "-").strip() or "-"

                if history_text:
                    history_entries.append(
                        {
                            "timestamp": timestamp,
                            "app_name": app_name,
                            "user_id": user_id,
                            "session_id": session_label,
                            "author": author,
                            "event_id": event_id or "-",
                            "text": history_text,
                        }
                    )

                # Keep long-term memory focused on user-origin durable facts only.
                if memory_text and _is_user_author(author):
                    category = _infer_fact_category(memory_text)
                    if category:
                        memory_entries.append(
                            {
                                "timestamp": timestamp,
                                "app_name": app_name,
                                "user_id": user_id,
                                "session_id": session_label,
                                "author": author,
                                "category": category,
                                "text": memory_text,
                            }
                        )

                if event_id:
                    new_event_ids.add(event_id)

            if not history_entries and not memory_entries:
                return

            self._append_history_entries(entries=history_entries)
            self._append_memory_entries(entries=memory_entries)
            if new_event_ids:
                known_event_ids.update(new_event_ids)
                self._save_event_ids(app_name=app_name, user_id=user_id, event_ids=known_event_ids)

    async def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[str],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Append explicit long-term memory items into MEMORY.md.

        ``custom_metadata`` can carry ``dialogue_timestamp`` and ``session_id`` for
        callers that want explicit timestamp/session attribution.
        """
        if not app_name or not user_id:
            return

        session_label = "-"
        if custom_metadata is not None:
            session_label = str(custom_metadata.get("session_id", "-") or "-")
        timestamp = _now_iso()
        if custom_metadata is not None:
            timestamp = _iso_from_unix_seconds(custom_metadata.get("dialogue_timestamp")) or timestamp

        entries: list[dict[str, str]] = []
        for raw in memories:
            text = (raw or "").strip()
            if not text:
                continue
            category = _infer_fact_category(text) or "context"
            entries.append(
                {
                    "timestamp": timestamp,
                    "app_name": app_name,
                    "user_id": user_id,
                    "session_id": session_label,
                    "author": "memory",
                    "category": category,
                    "text": text,
                }
            )
        if not entries:
            return

        with self._lock:
            self._ensure_root()
            self._append_memory_entries(entries=entries)

    async def search_memory(self, *, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        """Search MEMORY.md for app/user-scoped long-term facts."""
        response = SearchMemoryResponse()
        if not app_name or not user_id:
            return response

        memory_path = self._memory_file()
        if not memory_path.exists():
            return response

        query_tokens = _tokenize(query)
        if not query_tokens:
            return response

        with self._lock:
            lines = memory_path.read_text(encoding="utf-8").splitlines()

        for line in lines:
            parsed = self._parse_memory_line(line)
            if not parsed:
                continue
            row_app, row_user, author, timestamp, text = parsed
            if row_app and row_app != app_name:
                continue
            if row_user and row_user != user_id:
                continue
            if query_tokens.isdisjoint(_tokenize(text)):
                continue
            response.memories.append(
                MemoryEntry(
                    author=author,
                    timestamp=timestamp,
                    content=types.Content(role="user", parts=[types.Part(text=text)]),
                )
            )

        return response
