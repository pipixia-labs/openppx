"""SQLite session service factory for ADK runner."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.adk.sessions import DatabaseSessionService

from ..core.auth_paths import resolve_current_agent_dir


@dataclass(slots=True)
class SessionConfig:
    """Runtime session storage configuration (SQLite only)."""

    db_url: str


def _default_sqlite_db_url(agent_dir: Path | None = None) -> str:
    root = (agent_dir or resolve_current_agent_dir()).expanduser().resolve(strict=False)
    db_path = root / "sessions" / "sessions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path}"


def load_session_config() -> SessionConfig:
    db_url = os.getenv("OPENHERON_SESSION_DB_URL", "").strip() or _default_sqlite_db_url()
    return SessionConfig(db_url=db_url)


class AgentScopedSessionService:
    """Route session-service calls into per-agent SQLite stores."""

    def __init__(self, explicit_db_url: str = "") -> None:
        self._explicit_db_url = explicit_db_url.strip()
        self._services_by_key: dict[str, DatabaseSessionService] = {}

    def _service_for_current_agent(self) -> DatabaseSessionService:
        if self._explicit_db_url:
            key = f"explicit:{self._explicit_db_url}"
            db_url = self._explicit_db_url
        else:
            agent_dir = resolve_current_agent_dir()
            key = str(agent_dir.expanduser().resolve(strict=False))
            db_url = _default_sqlite_db_url(agent_dir=agent_dir)
        service = self._services_by_key.get(key)
        if service is None:
            service = DatabaseSessionService(db_url)
            self._services_by_key[key] = service
        return service

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._service_for_current_agent(), name)
        if callable(target):
            def _wrapped(*args: Any, **kwargs: Any) -> Any:
                return getattr(self._service_for_current_agent(), name)(*args, **kwargs)

            return _wrapped
        return target


def create_session_service(config: SessionConfig | None = None) -> Any:
    """Create ADK SQLite session service."""
    if config is not None:
        return DatabaseSessionService(config.db_url)
    explicit_db_url = os.getenv("OPENHERON_SESSION_DB_URL", "").strip()
    return AgentScopedSessionService(explicit_db_url=explicit_db_url)
