"""SQLite-backed token usage event storage and aggregation helpers."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.config import get_data_dir


def token_usage_db_path() -> Path:
    """Return SQLite path used for token usage events."""
    return get_data_dir() / "token_usage.db"


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with pragmatic defaults for small event writes."""
    path = db_path or token_usage_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_token_usage_schema(db_path: Path | None = None) -> None:
    """Create token usage tables/indexes when missing."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_token_usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_at TEXT NOT NULL,
                request_at_ms INTEGER NOT NULL,
                response_at TEXT NOT NULL,
                response_at_ms INTEGER NOT NULL,
                provider TEXT,
                model TEXT,
                session_id TEXT,
                invocation_id TEXT,
                request_tokens INTEGER NOT NULL,
                response_tokens INTEGER NOT NULL,
                request_text_tokens INTEGER NOT NULL,
                response_text_tokens INTEGER NOT NULL,
                request_image_tokens INTEGER NOT NULL,
                response_image_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                raw_usage_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_token_usage_events_response_at_ms "
            "ON llm_token_usage_events(response_at_ms DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_token_usage_events_provider "
            "ON llm_token_usage_events(provider)"
        )


def _value_of(obj: Any, key: str, default: Any = None) -> Any:
    """Read one field from dict/object containers."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce loose numeric values to int."""
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _modality_name(raw: Any) -> str:
    """Normalize a usage detail modality/type marker to lowercase text."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        named = getattr(raw, "name", None)
        if isinstance(named, str):
            return named.strip().lower()
        return str(raw).strip().lower()
    return raw.strip().lower()


def _count_by_modality(details: Any) -> tuple[int, int]:
    """Return (text_tokens, image_tokens) from usage detail rows."""
    if not isinstance(details, list):
        return 0, 0
    text_tokens = 0
    image_tokens = 0
    for item in details:
        token_count = _safe_int(
            _value_of(item, "token_count", _value_of(item, "tokens", 0)),
            default=0,
        )
        marker = _modality_name(
            _value_of(item, "modality", _value_of(item, "type", "")),
        )
        if "image" in marker:
            image_tokens += token_count
        elif "text" in marker:
            text_tokens += token_count
    return text_tokens, image_tokens


def extract_usage_tokens(llm_response: Any) -> dict[str, int]:
    """Extract token usage counters from ADK/LiteLLM/OpenAI-like response payloads.

    This parser prefers ADK/Gemini ``usage_metadata`` fields and then falls back
    to generic ``usage`` payloads commonly returned by OpenAI-compatible providers.
    """

    usage_metadata = _value_of(llm_response, "usage_metadata")
    usage = _value_of(llm_response, "usage")

    request_tokens = _safe_int(_value_of(usage_metadata, "prompt_token_count"))
    response_tokens = _safe_int(_value_of(usage_metadata, "candidates_token_count"))
    total_tokens = _safe_int(_value_of(usage_metadata, "total_token_count"))

    request_text_tokens, request_image_tokens = _count_by_modality(
        _value_of(usage_metadata, "prompt_tokens_details"),
    )
    response_text_tokens, response_image_tokens = _count_by_modality(
        _value_of(usage_metadata, "candidates_tokens_details"),
    )

    if request_tokens <= 0:
        request_tokens = _safe_int(_value_of(usage, "prompt_tokens", _value_of(usage, "input_tokens", 0)))
    if response_tokens <= 0:
        response_tokens = _safe_int(
            _value_of(usage, "completion_tokens", _value_of(usage, "output_tokens", 0))
        )
    if total_tokens <= 0:
        total_tokens = _safe_int(_value_of(usage, "total_tokens", 0))

    if request_text_tokens <= 0:
        request_text_tokens = request_tokens
    if response_text_tokens <= 0:
        response_text_tokens = response_tokens

    if total_tokens <= 0:
        total_tokens = max(0, request_tokens) + max(0, response_tokens)

    return {
        "request_tokens": max(0, request_tokens),
        "response_tokens": max(0, response_tokens),
        "request_text_tokens": max(0, request_text_tokens),
        "response_text_tokens": max(0, response_text_tokens),
        "request_image_tokens": max(0, request_image_tokens),
        "response_image_tokens": max(0, response_image_tokens),
        "total_tokens": max(0, total_tokens),
    }


def write_token_usage_event(event: dict[str, Any], db_path: Path | None = None) -> None:
    """Persist one token usage event row."""
    ensure_token_usage_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO llm_token_usage_events (
                request_at,
                request_at_ms,
                response_at,
                response_at_ms,
                provider,
                model,
                session_id,
                invocation_id,
                request_tokens,
                response_tokens,
                request_text_tokens,
                response_text_tokens,
                request_image_tokens,
                response_image_tokens,
                total_tokens,
                raw_usage_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event.get("request_at", "")),
                _safe_int(event.get("request_at_ms"), default=int(time.time() * 1000)),
                str(event.get("response_at", "")),
                _safe_int(event.get("response_at_ms"), default=int(time.time() * 1000)),
                str(event.get("provider", "")),
                str(event.get("model", "")),
                str(event.get("session_id", "")),
                str(event.get("invocation_id", "")),
                _safe_int(event.get("request_tokens")),
                _safe_int(event.get("response_tokens")),
                _safe_int(event.get("request_text_tokens")),
                _safe_int(event.get("response_text_tokens")),
                _safe_int(event.get("request_image_tokens")),
                _safe_int(event.get("response_image_tokens")),
                _safe_int(event.get("total_tokens")),
                json.dumps(event.get("raw_usage", {}), ensure_ascii=False, default=str),
            ),
        )


def parse_time_filter_to_epoch_ms(raw: str | None) -> int | None:
    """Parse ISO8601-like timestamp to epoch milliseconds."""
    value = str(raw or "").strip()
    if not value:
        return None
    normalized = value
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.fromisoformat(normalized)
    return int(dt.timestamp() * 1000)


def read_token_usage_stats(
    *,
    limit: int = 20,
    provider: str | None = None,
    since_ms: int | None = None,
    until_ms: int | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Read summary counters and recent token usage events."""
    ensure_token_usage_schema(db_path)
    with _connect(db_path) as conn:
        conditions: list[str] = []
        params: list[Any] = []
        if provider:
            conditions.append("provider = ?")
            params.append(provider)
        if since_ms is not None:
            conditions.append("response_at_ms >= ?")
            params.append(int(since_ms))
        if until_ms is not None:
            conditions.append("response_at_ms <= ?")
            params.append(int(until_ms))
        where = ""
        if conditions:
            where = " WHERE " + " AND ".join(conditions)

        totals = conn.execute(
            (
                "SELECT "
                "COUNT(*) AS requests, "
                "COALESCE(SUM(request_tokens), 0) AS request_tokens, "
                "COALESCE(SUM(response_tokens), 0) AS response_tokens, "
                "COALESCE(SUM(request_text_tokens), 0) AS request_text_tokens, "
                "COALESCE(SUM(response_text_tokens), 0) AS response_text_tokens, "
                "COALESCE(SUM(request_image_tokens), 0) AS request_image_tokens, "
                "COALESCE(SUM(response_image_tokens), 0) AS response_image_tokens, "
                "COALESCE(SUM(total_tokens), 0) AS total_tokens "
                "FROM llm_token_usage_events"
                f"{where}"
            ),
            params,
        ).fetchone()

        recent_rows = conn.execute(
            (
                "SELECT response_at, provider, model, session_id, invocation_id, "
                "request_tokens, response_tokens, request_text_tokens, response_text_tokens, "
                "request_image_tokens, response_image_tokens, total_tokens "
                "FROM llm_token_usage_events"
                f"{where} "
                "ORDER BY response_at_ms DESC "
                "LIMIT ?"
            ),
            [*params, max(1, int(limit))],
        ).fetchall()

    return {
        "requests": int(totals["requests"]) if totals else 0,
        "request_tokens": int(totals["request_tokens"]) if totals else 0,
        "response_tokens": int(totals["response_tokens"]) if totals else 0,
        "request_text_tokens": int(totals["request_text_tokens"]) if totals else 0,
        "response_text_tokens": int(totals["response_text_tokens"]) if totals else 0,
        "request_image_tokens": int(totals["request_image_tokens"]) if totals else 0,
        "response_image_tokens": int(totals["response_image_tokens"]) if totals else 0,
        "total_tokens": int(totals["total_tokens"]) if totals else 0,
        "since_ms": int(since_ms) if since_ms is not None else None,
        "until_ms": int(until_ms) if until_ms is not None else None,
        "recent": [dict(row) for row in recent_rows],
    }
