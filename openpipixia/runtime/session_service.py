"""SQLite session service factory for ADK runner."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.adk.sessions import DatabaseSessionService
from ..core.config import get_data_dir


@dataclass(slots=True)
class SessionConfig:
    """Runtime session storage configuration (SQLite only)."""

    db_url: str


def _default_sqlite_db_url() -> str:
    db_path = get_data_dir() / "database" / "sessions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path}"


def load_session_config() -> SessionConfig:
    db_url = os.getenv("OPENPIPIXIA_SESSION_DB_URL", "").strip() or _default_sqlite_db_url()
    return SessionConfig(db_url=db_url)


def create_session_service(config: SessionConfig | None = None) -> Any:
    """Create ADK SQLite session service."""
    cfg = config or load_session_config()
    return DatabaseSessionService(cfg.db_url)
