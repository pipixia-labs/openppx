"""Provider checkpoint migration catalog helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .checkpoint_schema import (
    CheckpointMigrationSpec,
    CheckpointSchemaRegistry,
    CheckpointSchemaSpec,
    DEFAULT_CHECKPOINT_SCHEMA_REGISTRY,
)


@dataclass(frozen=True, slots=True)
class CheckpointMigrationCatalogEntry:
    """Catalog entry for one provider-owned checkpoint schema."""

    provider_name: str
    spec: CheckpointSchemaSpec
    migrations: tuple[CheckpointMigrationSpec, ...] = field(default_factory=tuple)
    description: str = ""

    def payload(self) -> dict[str, Any]:
        """Return an inspectable description of this catalog entry."""
        return {
            "provider_name": self.provider_name,
            "runner_name": self.spec.runner_name,
            "checkpoint_type": self.spec.checkpoint_type,
            "payload_schema": self.spec.payload_schema,
            "payload_schema_version": self.spec.payload_schema_version,
            "migration_edges": [
                {
                    "from_version": migration.from_version,
                    "to_version": migration.to_version,
                    "payload_schema": migration.payload_schema,
                }
                for migration in self.migrations
            ],
            "description": self.description,
        }


class CheckpointMigrationCatalog:
    """Small explicit registry for provider checkpoint migration rules."""

    def __init__(self, entries: list[CheckpointMigrationCatalogEntry] | None = None) -> None:
        self._entries: dict[tuple[str, str, str, str], CheckpointMigrationCatalogEntry] = {}
        for entry in entries or []:
            self.register(entry)

    def register(self, entry: CheckpointMigrationCatalogEntry) -> None:
        """Register or replace one provider checkpoint migration entry."""
        key = (
            str(entry.provider_name or "").strip(),
            str(entry.spec.runner_name or "").strip(),
            str(entry.spec.checkpoint_type or "").strip(),
            str(entry.spec.payload_schema or "").strip(),
        )
        if not key[0] or not key[3]:
            raise ValueError("provider_name and payload_schema are required for checkpoint migration catalog entries")
        for migration in entry.migrations:
            if migration.payload_schema != entry.spec.payload_schema:
                raise ValueError("checkpoint migration payload_schema must match the catalog spec")
        self._entries[key] = entry

    def entries(self) -> list[CheckpointMigrationCatalogEntry]:
        """Return entries in deterministic order."""
        return [self._entries[key] for key in sorted(self._entries)]

    def apply_to_registry(self, registry: CheckpointSchemaRegistry) -> None:
        """Register all catalog specs and migration edges into a schema registry."""
        for entry in self.entries():
            registry.register(entry.spec)
            for migration in entry.migrations:
                registry.register_migration(migration)

    def payload(self) -> dict[str, Any]:
        """Return a JSON-serializable catalog payload."""
        return {"entries": [entry.payload() for entry in self.entries()]}


DEFAULT_CHECKPOINT_MIGRATION_CATALOG = CheckpointMigrationCatalog()
OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA = "openppx.browser_remote.job_checkpoint"
OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION = 2
_DEFAULT_BROWSER_REMOTE_CATALOG_APPLIED = False


def register_default_checkpoint_migration_entry(entry: CheckpointMigrationCatalogEntry) -> None:
    """Register one provider checkpoint migration entry in the default catalog and registry."""
    DEFAULT_CHECKPOINT_MIGRATION_CATALOG.register(entry)
    DEFAULT_CHECKPOINT_SCHEMA_REGISTRY.register(entry.spec)
    for migration in entry.migrations:
        DEFAULT_CHECKPOINT_SCHEMA_REGISTRY.register_migration(migration)


def ensure_default_checkpoint_migration_catalog_applied() -> None:
    """Register openppx-owned default provider checkpoint migrations once.

    Third-party providers should still register their own schema-specific
    entries. This default entry exists only for the stable browser remote
    checkpoint schema that openppx itself documents for provider contract tests.
    """
    global _DEFAULT_BROWSER_REMOTE_CATALOG_APPLIED
    if _DEFAULT_BROWSER_REMOTE_CATALOG_APPLIED:
        return
    register_default_checkpoint_migration_entry(
        CheckpointMigrationCatalogEntry(
            provider_name="openppx.browser_remote",
            spec=CheckpointSchemaSpec(
                runner_name="browser_remote",
                checkpoint_type="browser_remote_job_state",
                payload_schema=OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA,
                payload_schema_version=OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION,
                normalize_payload=_normalize_openppx_browser_remote_job_checkpoint,
            ),
            migrations=(
                CheckpointMigrationSpec(
                    runner_name="browser_remote",
                    checkpoint_type="browser_remote_job_state",
                    payload_schema=OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA,
                    from_version=1,
                    to_version=2,
                    migrate_payload=_migrate_openppx_browser_remote_job_checkpoint_v1_to_v2,
                ),
            ),
            description="Default openppx browser remote provider contract checkpoint schema.",
        )
    )
    _DEFAULT_BROWSER_REMOTE_CATALOG_APPLIED = True


def _normalize_openppx_browser_remote_job_checkpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the openppx browser remote checkpoint contract payload."""
    normalized = dict(payload)
    schema = str(normalized.get("schema") or "").strip()
    if schema and schema != OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA:
        raise ValueError(
            "unsupported openppx browser remote checkpoint schema "
            f"{normalized.get('schema')!r}"
        )
    version = _optional_int(normalized.get("schema_version"))
    if version != OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION:
        raise ValueError(
            "unsupported openppx browser remote checkpoint schema_version "
            f"{normalized.get('schema_version')!r}"
        )
    normalized["schema"] = OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA
    normalized["schema_version"] = OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION
    _copy_alias(normalized, source="jobId", target="job_id")
    _copy_alias(normalized, source="pageUrl", target="current_url")
    _copy_alias(normalized, source="currentUrl", target="current_url")
    _copy_alias(normalized, source="outputOffset", target="output_offset")
    if "output_offset" in normalized:
        normalized["output_offset"] = max(0, _optional_int(normalized.get("output_offset")) or 0)
    return normalized


def _migrate_openppx_browser_remote_job_checkpoint_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """Migrate the first openppx browser remote checkpoint contract to v2."""
    migrated = dict(payload)
    migrated["schema"] = OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA
    migrated["schema_version"] = OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION
    _copy_alias(migrated, source="jobId", target="job_id")
    _copy_alias(migrated, source="pageUrl", target="current_url")
    _copy_alias(migrated, source="currentUrl", target="current_url")
    _copy_alias(migrated, source="outputOffset", target="output_offset")
    migrated.setdefault("output_offset", 0)
    migrated["output_offset"] = max(0, _optional_int(migrated.get("output_offset")) or 0)
    return migrated


def _copy_alias(payload: dict[str, Any], *, source: str, target: str) -> None:
    if target not in payload and source in payload:
        payload[target] = payload[source]


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


__all__ = [
    "CheckpointMigrationCatalog",
    "CheckpointMigrationCatalogEntry",
    "DEFAULT_CHECKPOINT_MIGRATION_CATALOG",
    "OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA",
    "OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION",
    "ensure_default_checkpoint_migration_catalog_applied",
    "register_default_checkpoint_migration_entry",
]
