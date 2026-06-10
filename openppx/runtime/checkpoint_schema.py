"""Checkpoint payload schema registry for TaskRun checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from typing import Any

from ..gui.checkpoint import (
    GUI_TASK_CHECKPOINT_SCHEMA,
    GUI_TASK_CHECKPOINT_SCHEMA_VERSION,
    normalize_gui_task_checkpoint,
)


TASK_CHECKPOINT_ENVELOPE_SCHEMA = "openppx.task_checkpoint_payload"
TASK_CHECKPOINT_ENVELOPE_SCHEMA_VERSION = 1
TASK_CHECKPOINT_METADATA_KEY = "_checkpoint"

CheckpointNormalizer = Callable[[dict[str, Any]], dict[str, Any]]
CheckpointMigrator = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class CheckpointSchemaSpec:
    """Registered schema behavior for one runner/checkpoint type."""

    runner_name: str
    checkpoint_type: str
    payload_schema: str
    payload_schema_version: int
    normalize_payload: CheckpointNormalizer


@dataclass(frozen=True, slots=True)
class CheckpointMigrationSpec:
    """Registered migration edge for one provider-owned checkpoint schema."""

    runner_name: str
    checkpoint_type: str
    payload_schema: str
    from_version: int
    to_version: int
    migrate_payload: CheckpointMigrator


class CheckpointSchemaRegistry:
    """Resolve checkpoint payload schemas by runner and checkpoint type."""

    def __init__(
        self,
        specs: list[CheckpointSchemaSpec] | tuple[CheckpointSchemaSpec, ...] = (),
        migrations: list[CheckpointMigrationSpec] | tuple[CheckpointMigrationSpec, ...] = (),
    ) -> None:
        self._specs: dict[tuple[str, str], CheckpointSchemaSpec] = {}
        self._schema_specs: dict[tuple[str, str, str], CheckpointSchemaSpec] = {}
        self._migrations: dict[tuple[str, str, str], dict[int, list[CheckpointMigrationSpec]]] = {}
        for spec in specs:
            self.register(spec)
        for migration in migrations:
            self.register_migration(migration)

    def register(self, spec: CheckpointSchemaSpec) -> None:
        """Register or replace one checkpoint schema spec."""
        runner_key = _normalize_key(spec.runner_name)
        type_key = _normalize_key(spec.checkpoint_type)
        schema_key = _normalize_key(spec.payload_schema)
        key = (runner_key, type_key)
        self._specs[key] = spec
        if schema_key:
            self._schema_specs[(runner_key, type_key, schema_key)] = spec

    def register_migration(self, migration: CheckpointMigrationSpec) -> None:
        """Register or replace one checkpoint schema migration edge."""
        from_version = int(migration.from_version)
        to_version = int(migration.to_version)
        if from_version <= 0 or to_version <= 0:
            raise ValueError("checkpoint migration versions must be positive integers")
        if from_version == to_version:
            raise ValueError("checkpoint migration edge must change schema version")
        key = (
            _normalize_key(migration.runner_name),
            _normalize_key(migration.checkpoint_type),
            _normalize_key(migration.payload_schema),
        )
        outgoing = self._migrations.setdefault(key, {}).setdefault(from_version, [])
        outgoing[:] = [
            existing
            for existing in outgoing
            if int(existing.to_version) != to_version
        ]
        outgoing.append(migration)

    def resolve(
        self,
        *,
        runner_name: str,
        checkpoint_type: str,
        payload_schema: str | None = None,
    ) -> CheckpointSchemaSpec | None:
        """Return the most specific schema spec for a runner/checkpoint type."""
        runner_key = _normalize_key(runner_name)
        type_key = _normalize_key(checkpoint_type)
        schema_key = _normalize_key(payload_schema)
        if schema_key:
            for key in _migration_lookup_keys(
                runner_name=runner_key,
                checkpoint_type=type_key,
                payload_schema=schema_key,
            ):
                spec = self._schema_specs.get(key)
                if spec is not None:
                    return spec
        return (
            self._specs.get((runner_key, type_key))
            or self._specs.get((runner_key, ""))
            or self._specs.get(("", type_key))
        )

    def normalize_payload(
        self,
        *,
        runner_name: str,
        checkpoint_type: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Return a payload with stable checkpoint metadata.

        Runner state remains at the top level for backwards-compatible resume
        code. Framework-level schema information is stored under
        ``_checkpoint`` to avoid colliding with runner-specific fields.
        """
        raw = dict(payload or {})
        existing = raw.get(TASK_CHECKPOINT_METADATA_KEY)
        if isinstance(existing, dict) and existing:
            _validate_checkpoint_metadata(existing)
            payload_without_metadata = dict(raw)
            payload_without_metadata.pop(TASK_CHECKPOINT_METADATA_KEY, None)
            source_schema = _payload_schema(payload_without_metadata, metadata=existing, default_schema="")
            spec = self.resolve(
                runner_name=runner_name,
                checkpoint_type=checkpoint_type,
                payload_schema=source_schema,
            )
            if spec is None:
                return raw
            normalized, migration_path = self._normalize_with_spec(
                spec=spec,
                runner_name=runner_name,
                checkpoint_type=checkpoint_type,
                payload=payload_without_metadata,
                metadata=existing,
            )
            refreshed_metadata = _checkpoint_metadata(
                runner_name=runner_name,
                checkpoint_type=checkpoint_type,
                payload_schema=spec.payload_schema,
                payload_schema_version=spec.payload_schema_version,
                migration_path=migration_path,
            )
            if not migration_path and isinstance(existing.get("migration_path"), list):
                refreshed_metadata["migration_path"] = [str(item) for item in existing["migration_path"]]
            normalized[TASK_CHECKPOINT_METADATA_KEY] = refreshed_metadata
            return normalized

        source_schema = _payload_schema(raw, metadata=None, default_schema="")
        spec = self.resolve(
            runner_name=runner_name,
            checkpoint_type=checkpoint_type,
            payload_schema=source_schema,
        )
        migration_path: list[tuple[int, int]] = []
        if spec is not None:
            normalized, migration_path = self._normalize_with_spec(
                spec=spec,
                runner_name=runner_name,
                checkpoint_type=checkpoint_type,
                payload=raw,
                metadata=None,
            )
        else:
            normalized = raw
        normalized = dict(normalized)
        normalized[TASK_CHECKPOINT_METADATA_KEY] = _checkpoint_metadata(
            runner_name=runner_name,
            checkpoint_type=checkpoint_type,
            payload_schema=spec.payload_schema if spec is not None else "",
            payload_schema_version=spec.payload_schema_version if spec is not None else None,
            migration_path=migration_path,
        )
        return normalized

    def _normalize_with_spec(
        self,
        *,
        spec: CheckpointSchemaSpec,
        runner_name: str,
        checkpoint_type: str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[tuple[int, int]]]:
        """Apply registered migrations before spec-specific normalization."""
        migrated, migration_path = self._migrate_payload_to_spec(
            spec=spec,
            runner_name=runner_name,
            checkpoint_type=checkpoint_type,
            payload=payload,
            metadata=metadata,
        )
        return spec.normalize_payload(migrated), migration_path

    def _migrate_payload_to_spec(
        self,
        *,
        spec: CheckpointSchemaSpec,
        runner_name: str,
        checkpoint_type: str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[tuple[int, int]]]:
        """Migrate provider-owned payload schema versions when a path exists."""
        target_schema = _normalize_key(spec.payload_schema)
        target_version = int(spec.payload_schema_version)
        source_schema = _payload_schema(payload, metadata=metadata, default_schema=target_schema)
        source_version = _payload_schema_version(payload, metadata=metadata)
        if not target_schema or not source_schema or source_schema != target_schema or source_version is None:
            return dict(payload), []
        if source_version == target_version:
            return dict(payload), []
        if source_version > target_version:
            return dict(payload), []
        path = self._find_migration_path(
            runner_name=runner_name,
            checkpoint_type=checkpoint_type,
            payload_schema=target_schema,
            from_version=source_version,
            to_version=target_version,
        )
        if not path:
            raise ValueError(
                "no checkpoint migration path: "
                f"runner={_normalize_key(runner_name)!r} checkpoint_type={_normalize_key(checkpoint_type)!r} "
                f"schema={target_schema!r} from={source_version!r} to={target_version!r}"
            )
        migrated = dict(payload)
        migration_path: list[tuple[int, int]] = []
        for edge in path:
            migrated = dict(edge.migrate_payload(dict(migrated)))
            migrated["schema"] = target_schema
            migrated["schema_version"] = int(edge.to_version)
            migration_path.append((int(edge.from_version), int(edge.to_version)))
        return migrated, migration_path

    def _find_migration_path(
        self,
        *,
        runner_name: str,
        checkpoint_type: str,
        payload_schema: str,
        from_version: int,
        to_version: int,
    ) -> list[CheckpointMigrationSpec]:
        """Return a shortest registered migration path between versions."""
        if from_version == to_version:
            return []
        edges: dict[int, list[CheckpointMigrationSpec]] = {}
        for key in _migration_lookup_keys(
            runner_name=runner_name,
            checkpoint_type=checkpoint_type,
            payload_schema=payload_schema,
        ):
            for source_version, outgoing in self._migrations.get(key, {}).items():
                edges.setdefault(source_version, []).extend(outgoing)
        if not edges:
            return []
        queue: deque[tuple[int, list[CheckpointMigrationSpec]]] = deque([(from_version, [])])
        visited = {from_version}
        while queue:
            current_version, path = queue.popleft()
            outgoing = edges.get(current_version, [])
            if not outgoing:
                continue
            for edge in outgoing:
                next_version = int(edge.to_version)
                next_path = [*path, edge]
                if next_version == to_version:
                    return next_path
                if next_version not in visited:
                    visited.add(next_version)
                    queue.append((next_version, next_path))
        return []


def normalize_task_checkpoint_payload(
    *,
    runner_name: str,
    checkpoint_type: str,
    payload: dict[str, Any] | None,
    registry: CheckpointSchemaRegistry | None = None,
) -> dict[str, Any]:
    """Normalize a runner checkpoint payload through the configured registry."""
    return (registry or DEFAULT_CHECKPOINT_SCHEMA_REGISTRY).normalize_payload(
        runner_name=runner_name,
        checkpoint_type=checkpoint_type,
        payload=payload,
    )


def checkpoint_metadata(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Return checkpoint metadata from a normalized payload, if present."""
    raw = payload.get(TASK_CHECKPOINT_METADATA_KEY) if isinstance(payload, dict) else None
    return dict(raw) if isinstance(raw, dict) else {}


def _checkpoint_metadata(
    *,
    runner_name: str,
    checkpoint_type: str,
    payload_schema: str,
    payload_schema_version: int | None,
    migration_path: list[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "schema": TASK_CHECKPOINT_ENVELOPE_SCHEMA,
        "schema_version": TASK_CHECKPOINT_ENVELOPE_SCHEMA_VERSION,
        "runner_name": _normalize_key(runner_name),
        "checkpoint_type": _normalize_key(checkpoint_type) or "runner",
        "payload_schema": str(payload_schema or "").strip(),
    }
    if payload_schema_version is not None:
        metadata["payload_schema_version"] = int(payload_schema_version)
    if migration_path:
        metadata["migration_path"] = [f"{source}->{target}" for source, target in migration_path]
    return metadata


def _validate_checkpoint_metadata(metadata: dict[str, Any]) -> None:
    schema = str(metadata.get("schema") or "").strip()
    if schema != TASK_CHECKPOINT_ENVELOPE_SCHEMA:
        raise ValueError(f"unsupported TaskRun checkpoint envelope schema {schema!r}")
    version = _maybe_int(metadata.get("schema_version"))
    if version != TASK_CHECKPOINT_ENVELOPE_SCHEMA_VERSION:
        raise ValueError(f"unsupported TaskRun checkpoint envelope schema_version {metadata.get('schema_version')!r}")


def _normalize_gui_runner_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a GUI runner checkpoint payload."""
    return normalize_gui_task_checkpoint(payload, include_schema=True)


def _normalize_key(value: Any) -> str:
    return str(value or "").strip()


def _maybe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _payload_schema(payload: dict[str, Any], *, metadata: dict[str, Any] | None, default_schema: str) -> str:
    schema = _normalize_key(payload.get("schema"))
    if schema:
        return schema
    metadata_schema = _normalize_key((metadata or {}).get("payload_schema"))
    return metadata_schema or default_schema


def _payload_schema_version(payload: dict[str, Any], *, metadata: dict[str, Any] | None) -> int | None:
    version = _maybe_int(payload.get("schema_version"))
    if version is not None:
        return version
    alias_version = _maybe_int(payload.get("schemaVersion"))
    if alias_version is not None:
        return alias_version
    return _maybe_int((metadata or {}).get("payload_schema_version"))


def _migration_lookup_keys(
    *,
    runner_name: str,
    checkpoint_type: str,
    payload_schema: str,
) -> tuple[tuple[str, str, str], ...]:
    runner_key = _normalize_key(runner_name)
    type_key = _normalize_key(checkpoint_type)
    schema_key = _normalize_key(payload_schema)
    return (
        (runner_key, type_key, schema_key),
        (runner_key, "", schema_key),
        ("", type_key, schema_key),
        ("", "", schema_key),
    )


DEFAULT_CHECKPOINT_SCHEMA_REGISTRY = CheckpointSchemaRegistry(
    specs=(
        CheckpointSchemaSpec(
            runner_name="gui_job",
            checkpoint_type="gui_runner_state",
            payload_schema=GUI_TASK_CHECKPOINT_SCHEMA,
            payload_schema_version=GUI_TASK_CHECKPOINT_SCHEMA_VERSION,
            normalize_payload=_normalize_gui_runner_payload,
        ),
        CheckpointSchemaSpec(
            runner_name="gui_job",
            checkpoint_type="",
            payload_schema=GUI_TASK_CHECKPOINT_SCHEMA,
            payload_schema_version=GUI_TASK_CHECKPOINT_SCHEMA_VERSION,
            normalize_payload=_normalize_gui_runner_payload,
        ),
    )
)


__all__ = [
    "CheckpointMigrationSpec",
    "CheckpointSchemaRegistry",
    "CheckpointSchemaSpec",
    "DEFAULT_CHECKPOINT_SCHEMA_REGISTRY",
    "TASK_CHECKPOINT_ENVELOPE_SCHEMA",
    "TASK_CHECKPOINT_ENVELOPE_SCHEMA_VERSION",
    "TASK_CHECKPOINT_METADATA_KEY",
    "checkpoint_metadata",
    "normalize_task_checkpoint_payload",
]
