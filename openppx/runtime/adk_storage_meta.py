"""Sidecar metadata guard for ADK-owned persistent storage."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adk_version import installed_adk_version

_META_SCHEMA_VERSION = 1
_META_FILE_NAME = ".adk_meta.json"
_LAST_WRITER = "openppx"


def _major_version(version: str) -> int | None:
    """Return the leading integer version component when it is parseable."""
    head = (version or "").split(".", 1)[0]
    try:
        return int(head)
    except (TypeError, ValueError):
        return None


def _current_adk_meta() -> dict[str, Any]:
    """Build the current storage metadata payload."""
    version = installed_adk_version()
    major = _major_version(version)
    if major is None:
        raise RuntimeError(f"Cannot determine google-adk major version from {version!r}.")
    return {
        "schema_version": _META_SCHEMA_VERSION,
        "adk_major": major,
        "adk_version": version,
        "last_writer": _LAST_WRITER,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def adk_storage_meta_path(data_dir: str | Path) -> Path:
    """Return the sidecar metadata path for one openppx data directory."""
    return Path(data_dir).expanduser() / "database" / _META_FILE_NAME


def infer_data_dir_from_sqlite_path(db_path: str | Path) -> Path | None:
    """Infer the openppx data directory from a SQLite file path when possible."""
    path = Path(db_path).expanduser()
    if path.parent.name != "database":
        return None
    return path.parent.parent


def sqlite_path_from_db_url(db_url: str) -> Path | None:
    """Extract a local SQLite file path from a SQLAlchemy SQLite URL."""
    prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
    for prefix in prefixes:
        if db_url.startswith(prefix):
            raw = db_url[len(prefix) :]
            if raw:
                return Path(raw).expanduser()
    return None


def _read_meta(path: Path) -> dict[str, Any]:
    """Read one metadata JSON file or raise a clear runtime error."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid ADK storage metadata file at {path}.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid ADK storage metadata file at {path}.")
    return payload


def _write_meta(path: Path, payload: dict[str, Any]) -> None:
    """Write metadata atomically enough for local process startup races."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def ensure_adk_storage_meta(data_dir: str | Path) -> Path:
    """Ensure one data directory was not written by an incompatible ADK major."""
    path = adk_storage_meta_path(data_dir)
    current = _current_adk_meta()
    if not path.exists():
        _write_meta(path, current)
        return path

    stored = _read_meta(path)
    stored_major = stored.get("adk_major")
    current_major = current["adk_major"]
    if stored_major != current_major:
        raise RuntimeError(
            "ADK storage metadata mismatch: "
            f"{path} was last written with google-adk major {stored_major!r}, "
            f"but the current runtime is google-adk major {current_major!r}."
        )

    stored_schema = stored.get("schema_version", 0)
    if not isinstance(stored_schema, int) or stored_schema > _META_SCHEMA_VERSION:
        raise RuntimeError(
            "Unsupported ADK storage metadata schema "
            f"{stored_schema!r} in {path}; openppx supports schema {_META_SCHEMA_VERSION}."
        )

    _write_meta(path, current)
    return path


def ensure_adk_storage_meta_for_sqlite_path(db_path: str | Path) -> Path | None:
    """Ensure sidecar metadata for SQLite paths under an openppx database dir."""
    data_dir = infer_data_dir_from_sqlite_path(db_path)
    if data_dir is None:
        return None
    return ensure_adk_storage_meta(data_dir)


def ensure_adk_storage_meta_for_db_url(db_url: str) -> Path | None:
    """Ensure sidecar metadata for SQLite database URLs under openppx data dirs."""
    db_path = sqlite_path_from_db_url(db_url)
    if db_path is None:
        return None
    return ensure_adk_storage_meta_for_sqlite_path(db_path)
