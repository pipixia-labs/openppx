"""Durable registry for remote browser providers discovered at runtime."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .task_store import task_db_path


@dataclass(frozen=True, slots=True)
class BrowserRemoteProvider:
    """One observed remote browser provider endpoint."""

    provider_id: str
    target: str
    node: str
    proxy_url: str
    status: str
    capability_json: str
    last_error: str
    last_seen_at_ms: int
    updated_at_ms: int

    @property
    def capability(self) -> dict[str, Any]:
        """Return decoded provider capability payload."""
        return _json_loads(self.capability_json)


@dataclass(frozen=True, slots=True)
class BrowserRemoteJob:
    """One observed remote browser job returned by a provider."""

    job_record_id: str
    provider_id: str
    target: str
    node: str
    proxy_url: str
    action: str
    external_job_id: str
    status: str
    payload_json: str
    last_error: str
    last_seen_at_ms: int
    updated_at_ms: int

    @property
    def payload(self) -> dict[str, Any]:
        """Return decoded remote job payload."""
        return _json_loads(self.payload_json)


class BrowserRemoteProviderStore:
    """SQLite-backed registry for remote browser provider capability discovery.

    The registry is intentionally small: browser proxy calls remain the source
    of live truth, while this store gives other processes and future UI layers a
    durable view of which providers were observed and what they claimed to
    support.
    """

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path is not None else task_db_path()
        self._lock = threading.Lock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create browser provider registry tables when missing."""
        with _connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS browser_remote_providers (
                    provider_id TEXT PRIMARY KEY,
                    target TEXT NOT NULL,
                    node TEXT NOT NULL,
                    proxy_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    capability_json TEXT NOT NULL,
                    last_error TEXT NOT NULL,
                    last_seen_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_browser_remote_providers_target "
                "ON browser_remote_providers(target, updated_at_ms DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_browser_remote_providers_node "
                "ON browser_remote_providers(target, node, updated_at_ms DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS browser_remote_jobs (
                    job_record_id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    target TEXT NOT NULL,
                    node TEXT NOT NULL,
                    proxy_url TEXT NOT NULL,
                    action TEXT NOT NULL,
                    external_job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    last_error TEXT NOT NULL,
                    last_seen_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_browser_remote_jobs_provider "
                "ON browser_remote_jobs(provider_id, updated_at_ms DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_browser_remote_jobs_target "
                "ON browser_remote_jobs(target, status, updated_at_ms DESC)"
            )

    def record_observation(
        self,
        *,
        target: str,
        node: str = "",
        proxy_url: str,
        status: str,
        capability: dict[str, Any] | None = None,
        last_error: str = "",
    ) -> BrowserRemoteProvider:
        """Upsert one provider observation and return the stored row."""
        normalized_target = _normalize_target(target)
        normalized_node = str(node or "").strip()
        sanitized_url = _sanitize_proxy_url(proxy_url)
        normalized_status = _normalize_status(status)
        provider_id = _provider_id(
            target=normalized_target,
            node=normalized_node,
            proxy_url=sanitized_url,
        )
        now_ms = _now_ms()
        with self._lock, _connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO browser_remote_providers (
                    provider_id, target, node, proxy_url, status, capability_json,
                    last_error, last_seen_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    target = excluded.target,
                    node = excluded.node,
                    proxy_url = excluded.proxy_url,
                    status = excluded.status,
                    capability_json = excluded.capability_json,
                    last_error = excluded.last_error,
                    last_seen_at_ms = excluded.last_seen_at_ms,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    provider_id,
                    normalized_target,
                    normalized_node,
                    sanitized_url,
                    normalized_status,
                    _json_dumps(capability or {}),
                    str(last_error or "").strip(),
                    now_ms,
                    now_ms,
                ),
            )
            row = conn.execute(
                "SELECT * FROM browser_remote_providers WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
        assert row is not None
        return _provider_from_row(row)

    def list_providers(
        self,
        *,
        target: str | None = None,
        node: str | None = None,
        limit: int = 20,
    ) -> list[BrowserRemoteProvider]:
        """List recently observed remote browser providers."""
        safe_limit = max(1, min(int(limit or 20), 200))
        clauses: list[str] = []
        params: list[Any] = []
        normalized_target = _normalize_target(target or "") if str(target or "").strip() else ""
        if normalized_target:
            clauses.append("target = ?")
            params.append(normalized_target)
        if str(node or "").strip():
            clauses.append("node = ?")
            params.append(str(node or "").strip())
        params.append(safe_limit)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM browser_remote_providers
                {where}
                ORDER BY updated_at_ms DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_provider_from_row(row) for row in rows]

    def get_provider(self, provider_id: str) -> BrowserRemoteProvider | None:
        """Return one provider observation by id."""
        normalized = str(provider_id or "").strip()
        if not normalized:
            return None
        with _connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM browser_remote_providers WHERE provider_id = ?",
                (normalized,),
            ).fetchone()
        return _provider_from_row(row) if row is not None else None

    def record_job_observation(
        self,
        *,
        provider_id: str,
        target: str,
        node: str = "",
        proxy_url: str,
        action: str,
        external_job_id: str,
        status: str,
        payload: dict[str, Any] | None = None,
        last_error: str = "",
    ) -> BrowserRemoteJob:
        """Upsert one remote browser job observation and return the stored row."""
        normalized_target = _normalize_target(target)
        normalized_node = str(node or "").strip()
        sanitized_url = _sanitize_proxy_url(proxy_url)
        normalized_job_id = str(external_job_id or "").strip()
        if not normalized_job_id:
            raise ValueError("external_job_id is required")
        job_record_id = _job_record_id(
            provider_id=str(provider_id or "").strip(),
            external_job_id=normalized_job_id,
        )
        now_ms = _now_ms()
        with self._lock, _connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO browser_remote_jobs (
                    job_record_id, provider_id, target, node, proxy_url, action,
                    external_job_id, status, payload_json, last_error,
                    last_seen_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_record_id) DO UPDATE SET
                    provider_id = excluded.provider_id,
                    target = excluded.target,
                    node = excluded.node,
                    proxy_url = excluded.proxy_url,
                    action = excluded.action,
                    external_job_id = excluded.external_job_id,
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    last_error = excluded.last_error,
                    last_seen_at_ms = excluded.last_seen_at_ms,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    job_record_id,
                    str(provider_id or "").strip(),
                    normalized_target,
                    normalized_node,
                    sanitized_url,
                    str(action or "").strip().lower(),
                    normalized_job_id,
                    _normalize_job_status(status),
                    _json_dumps(payload or {}),
                    str(last_error or "").strip(),
                    now_ms,
                    now_ms,
                ),
            )
            row = conn.execute(
                "SELECT * FROM browser_remote_jobs WHERE job_record_id = ?",
                (job_record_id,),
            ).fetchone()
        assert row is not None
        return _job_from_row(row)

    def list_jobs(
        self,
        *,
        target: str | None = None,
        node: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[BrowserRemoteJob]:
        """List recently observed remote browser jobs."""
        safe_limit = max(1, min(int(limit or 20), 200))
        clauses: list[str] = []
        params: list[Any] = []
        normalized_target = _normalize_target(target or "") if str(target or "").strip() else ""
        if normalized_target:
            clauses.append("target = ?")
            params.append(normalized_target)
        if str(node or "").strip():
            clauses.append("node = ?")
            params.append(str(node or "").strip())
        if str(status or "").strip():
            clauses.append("status = ?")
            params.append(_normalize_job_status(str(status or "")))
        params.append(safe_limit)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM browser_remote_jobs
                {where}
                ORDER BY updated_at_ms DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def get_job(self, job_record_id: str) -> BrowserRemoteJob | None:
        """Return one remote browser job observation by id."""
        normalized = str(job_record_id or "").strip()
        if not normalized:
            return None
        with _connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM browser_remote_jobs WHERE job_record_id = ?",
                (normalized,),
            ).fetchone()
        return _job_from_row(row) if row is not None else None


def browser_remote_provider_payload(provider: BrowserRemoteProvider) -> dict[str, Any]:
    """Return JSON-serializable provider payload."""
    return {
        "provider_id": provider.provider_id,
        "target": provider.target,
        "node": provider.node,
        "proxy_url": provider.proxy_url,
        "status": provider.status,
        "capability": provider.capability,
        "last_error": provider.last_error,
        "last_seen_at_ms": provider.last_seen_at_ms,
        "updated_at_ms": provider.updated_at_ms,
    }


def browser_remote_job_payload(job: BrowserRemoteJob) -> dict[str, Any]:
    """Return JSON-serializable remote job payload."""
    return {
        "job_record_id": job.job_record_id,
        "provider_id": job.provider_id,
        "target": job.target,
        "node": job.node,
        "proxy_url": job.proxy_url,
        "action": job.action,
        "external_job_id": job.external_job_id,
        "status": job.status,
        "payload": job.payload,
        "last_error": job.last_error,
        "last_seen_at_ms": job.last_seen_at_ms,
        "updated_at_ms": job.updated_at_ms,
    }


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _provider_from_row(row: sqlite3.Row) -> BrowserRemoteProvider:
    return BrowserRemoteProvider(
        provider_id=str(row["provider_id"]),
        target=str(row["target"]),
        node=str(row["node"]),
        proxy_url=str(row["proxy_url"]),
        status=str(row["status"]),
        capability_json=str(row["capability_json"]),
        last_error=str(row["last_error"]),
        last_seen_at_ms=int(row["last_seen_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _job_from_row(row: sqlite3.Row) -> BrowserRemoteJob:
    return BrowserRemoteJob(
        job_record_id=str(row["job_record_id"]),
        provider_id=str(row["provider_id"]),
        target=str(row["target"]),
        node=str(row["node"]),
        proxy_url=str(row["proxy_url"]),
        action=str(row["action"]),
        external_job_id=str(row["external_job_id"]),
        status=str(row["status"]),
        payload_json=str(row["payload_json"]),
        last_error=str(row["last_error"]),
        last_seen_at_ms=int(row["last_seen_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _provider_id(*, target: str, node: str, proxy_url: str) -> str:
    digest = hashlib.sha256(f"{target}\0{node}\0{proxy_url}".encode("utf-8")).hexdigest()[:16]
    return f"browser_provider_{digest}"


def _job_record_id(*, provider_id: str, external_job_id: str) -> str:
    digest = hashlib.sha256(f"{provider_id}\0{external_job_id}".encode("utf-8")).hexdigest()[:16]
    return f"browser_job_{digest}"


def _sanitize_proxy_url(proxy_url: str) -> str:
    raw = str(proxy_url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw
    if "@" not in parsed.netloc:
        return raw
    safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, safe_netloc, parsed.path, parsed.query, parsed.fragment))


def _normalize_target(target: str) -> str:
    value = str(target or "").strip().lower()
    if value in {"node", "sandbox"}:
        return value
    return "node" if not value else value


def _normalize_status(status: str) -> str:
    value = str(status or "").strip().lower()
    return value if value in {"available", "degraded", "unavailable"} else "degraded"


def _normalize_job_status(status: str) -> str:
    value = str(status or "").strip().lower()
    aliases = {
        "pending": "queued",
        "created": "queued",
        "in_progress": "running",
        "processing": "running",
        "done": "completed",
        "succeeded": "completed",
        "success": "completed",
        "error": "failed",
        "aborted": "cancelled",
    }
    normalized = aliases.get(value, value)
    allowed = {"queued", "running", "paused", "waiting_user", "completed", "failed", "cancelled", "lost"}
    return normalized if normalized in allowed else "running"


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload if payload is not None else {}, ensure_ascii=False, default=str)


def _json_loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _now_ms() -> int:
    return int(time.time() * 1000)
