"""SQLite session service factory for ADK runner."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.adk.sessions import DatabaseSessionService
from ..core.config import get_data_dir
from .adk_storage_meta import ensure_adk_storage_meta
from .adk_storage_meta import ensure_adk_storage_meta_for_db_url


@dataclass(slots=True)
class SessionConfig:
    """Runtime session storage configuration (SQLite only)."""

    db_url: str


def _default_sqlite_db_url() -> str:
    data_dir = get_data_dir()
    ensure_adk_storage_meta(data_dir)
    db_path = data_dir / "database" / "sessions.db"
    return f"sqlite+aiosqlite:///{db_path}"


def load_session_config() -> SessionConfig:
    db_url = os.getenv("OPENPPX_SESSION_DB_URL", "").strip() or _default_sqlite_db_url()
    return SessionConfig(db_url=db_url)


def create_session_service(config: SessionConfig | None = None) -> Any:
    """Create ADK SQLite session service."""
    cfg = config or load_session_config()
    ensure_adk_storage_meta_for_db_url(cfg.db_url)
    return DatabaseSessionService(cfg.db_url)
