"""Tests for TaskRun checkpoint schema normalization."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openppx.gui.checkpoint import GUI_TASK_CHECKPOINT_SCHEMA, GUI_TASK_CHECKPOINT_SCHEMA_VERSION
from openppx.runtime.checkpoint_schema import (
    CheckpointMigrationSpec,
    CheckpointSchemaRegistry,
    CheckpointSchemaSpec,
    TASK_CHECKPOINT_ENVELOPE_SCHEMA,
    TASK_CHECKPOINT_ENVELOPE_SCHEMA_VERSION,
    TASK_CHECKPOINT_METADATA_KEY,
    normalize_task_checkpoint_payload,
)
from openppx.runtime.checkpoint_migration_catalog import (
    CheckpointMigrationCatalog,
    CheckpointMigrationCatalogEntry,
)
from openppx.runtime.task_execution import TaskController
from openppx.runtime.task_store import TaskStore


class TaskCheckpointSchemaTests(unittest.TestCase):
    def test_unknown_runner_preserves_payload_and_adds_envelope_metadata(self) -> None:
        payload = normalize_task_checkpoint_payload(
            runner_name="checkpoint_fake",
            checkpoint_type="runner_state",
            payload={"target_id": "tab-1", "next_step": 4},
        )

        self.assertEqual(payload["target_id"], "tab-1")
        self.assertEqual(payload["next_step"], 4)
        metadata = payload[TASK_CHECKPOINT_METADATA_KEY]
        self.assertEqual(metadata["schema"], TASK_CHECKPOINT_ENVELOPE_SCHEMA)
        self.assertEqual(metadata["schema_version"], TASK_CHECKPOINT_ENVELOPE_SCHEMA_VERSION)
        self.assertEqual(metadata["runner_name"], "checkpoint_fake")
        self.assertEqual(metadata["checkpoint_type"], "runner_state")
        self.assertEqual(metadata["payload_schema"], "")

    def test_gui_runner_uses_registered_gui_payload_schema(self) -> None:
        payload = normalize_task_checkpoint_payload(
            runner_name="gui_job",
            checkpoint_type="gui_runner_state",
            payload={"task": "GUI workflow", "history": [{"step": 1}], "next_step": 2},
        )

        self.assertEqual(payload["schema"], GUI_TASK_CHECKPOINT_SCHEMA)
        self.assertEqual(payload["schema_version"], GUI_TASK_CHECKPOINT_SCHEMA_VERSION)
        self.assertEqual(payload["task"], "GUI workflow")
        metadata = payload[TASK_CHECKPOINT_METADATA_KEY]
        self.assertEqual(metadata["payload_schema"], GUI_TASK_CHECKPOINT_SCHEMA)
        self.assertEqual(metadata["payload_schema_version"], GUI_TASK_CHECKPOINT_SCHEMA_VERSION)

    def test_record_task_checkpoint_rejects_unsupported_registered_payload_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(db_path=Path(tmp) / "tasks.db")
            task = store.create_task(kind="gui_task", status="running", title="GUI workflow")
            controller = TaskController(task_store=store)

            result = controller.record_task_checkpoint(
                task.task_id,
                checkpoint_type="gui_runner_state",
                runner_name="gui_job",
                checkpoint_payload={
                    "schema": GUI_TASK_CHECKPOINT_SCHEMA,
                    "schema_version": 999,
                    "task": "future",
                },
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["action"], "invalid_checkpoint_payload")
            self.assertIn("unsupported GUI task checkpoint", result["error"])

    def test_existing_envelope_still_validates_registered_payload_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported GUI task checkpoint"):
            normalize_task_checkpoint_payload(
                runner_name="gui_job",
                checkpoint_type="gui_runner_state",
                payload={
                    "schema": GUI_TASK_CHECKPOINT_SCHEMA,
                    "schema_version": 999,
                    "task": "future",
                    TASK_CHECKPOINT_METADATA_KEY: {
                        "schema": TASK_CHECKPOINT_ENVELOPE_SCHEMA,
                        "schema_version": TASK_CHECKPOINT_ENVELOPE_SCHEMA_VERSION,
                        "runner_name": "gui_job",
                        "checkpoint_type": "gui_runner_state",
                        "payload_schema": GUI_TASK_CHECKPOINT_SCHEMA,
                        "payload_schema_version": 999,
                    },
                },
            )

    def test_checkpoint_migration_graph_applies_multi_hop_provider_migration(self) -> None:
        def _normalize_provider(payload: dict) -> dict:
            if payload.get("schema") != "provider.browser.checkpoint":
                raise ValueError("unexpected provider checkpoint schema")
            if payload.get("schema_version") != 3:
                raise ValueError("unexpected provider checkpoint schema version")
            return {**payload, "normalized": True}

        registry = CheckpointSchemaRegistry(
            specs=[
                CheckpointSchemaSpec(
                    runner_name="browser_remote",
                    checkpoint_type="browser_remote_job_state",
                    payload_schema="provider.browser.checkpoint",
                    payload_schema_version=3,
                    normalize_payload=_normalize_provider,
                )
            ],
            migrations=[
                CheckpointMigrationSpec(
                    runner_name="browser_remote",
                    checkpoint_type="browser_remote_job_state",
                    payload_schema="provider.browser.checkpoint",
                    from_version=1,
                    to_version=2,
                    migrate_payload=lambda payload: {
                        **payload,
                        "schema_version": 2,
                        "url": payload["pageUrl"],
                    },
                ),
                CheckpointMigrationSpec(
                    runner_name="browser_remote",
                    checkpoint_type="browser_remote_job_state",
                    payload_schema="provider.browser.checkpoint",
                    from_version=2,
                    to_version=3,
                    migrate_payload=lambda payload: {
                        **payload,
                        "schema_version": 3,
                        "cursor": payload.get("cursor", 0),
                    },
                ),
            ],
        )

        payload = normalize_task_checkpoint_payload(
            runner_name="browser_remote",
            checkpoint_type="browser_remote_job_state",
            payload={
                "schema": "provider.browser.checkpoint",
                "schema_version": 1,
                "pageUrl": "https://example.test",
            },
            registry=registry,
        )

        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(payload["url"], "https://example.test")
        self.assertEqual(payload["cursor"], 0)
        self.assertTrue(payload["normalized"])
        metadata = payload[TASK_CHECKPOINT_METADATA_KEY]
        self.assertEqual(metadata["payload_schema"], "provider.browser.checkpoint")
        self.assertEqual(metadata["payload_schema_version"], 3)
        self.assertEqual(metadata["migration_path"], ["1->2", "2->3"])

    def test_checkpoint_migration_graph_rejects_missing_path(self) -> None:
        registry = CheckpointSchemaRegistry(
            specs=[
                CheckpointSchemaSpec(
                    runner_name="browser_remote",
                    checkpoint_type="browser_remote_job_state",
                    payload_schema="provider.browser.checkpoint",
                    payload_schema_version=3,
                    normalize_payload=lambda payload: payload,
                )
            ],
            migrations=[
                CheckpointMigrationSpec(
                    runner_name="browser_remote",
                    checkpoint_type="browser_remote_job_state",
                    payload_schema="provider.browser.checkpoint",
                    from_version=2,
                    to_version=3,
                    migrate_payload=lambda payload: {**payload, "schema_version": 3},
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "no checkpoint migration path"):
            normalize_task_checkpoint_payload(
                runner_name="browser_remote",
                checkpoint_type="browser_remote_job_state",
                payload={
                    "schema": "provider.browser.checkpoint",
                    "schema_version": 1,
                    "pageUrl": "https://example.test",
                },
                registry=registry,
            )

    def test_existing_envelope_metadata_is_refreshed_after_provider_migration(self) -> None:
        registry = CheckpointSchemaRegistry(
            specs=[
                CheckpointSchemaSpec(
                    runner_name="browser_remote",
                    checkpoint_type="browser_remote_job_state",
                    payload_schema="provider.browser.checkpoint",
                    payload_schema_version=2,
                    normalize_payload=lambda payload: payload,
                )
            ],
            migrations=[
                CheckpointMigrationSpec(
                    runner_name="browser_remote",
                    checkpoint_type="browser_remote_job_state",
                    payload_schema="provider.browser.checkpoint",
                    from_version=1,
                    to_version=2,
                    migrate_payload=lambda payload: {**payload, "schema_version": 2, "cursor": 1},
                )
            ],
        )

        payload = normalize_task_checkpoint_payload(
            runner_name="browser_remote",
            checkpoint_type="browser_remote_job_state",
            payload={
                "schema": "provider.browser.checkpoint",
                "schema_version": 1,
                "pageUrl": "https://example.test",
                TASK_CHECKPOINT_METADATA_KEY: {
                    "schema": TASK_CHECKPOINT_ENVELOPE_SCHEMA,
                    "schema_version": TASK_CHECKPOINT_ENVELOPE_SCHEMA_VERSION,
                    "runner_name": "browser_remote",
                    "checkpoint_type": "browser_remote_job_state",
                    "payload_schema": "provider.browser.checkpoint",
                    "payload_schema_version": 1,
                },
            },
            registry=registry,
        )

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["cursor"], 1)
        self.assertEqual(payload[TASK_CHECKPOINT_METADATA_KEY]["payload_schema_version"], 2)

    def test_checkpoint_migration_catalog_applies_provider_entry_to_registry(self) -> None:
        catalog = CheckpointMigrationCatalog(
            [
                CheckpointMigrationCatalogEntry(
                    provider_name="browser-provider",
                    spec=CheckpointSchemaSpec(
                        runner_name="browser_remote",
                        checkpoint_type="browser_remote_job_state",
                        payload_schema="provider.catalog.checkpoint",
                        payload_schema_version=2,
                        normalize_payload=lambda payload: payload,
                    ),
                    migrations=(
                        CheckpointMigrationSpec(
                            runner_name="browser_remote",
                            checkpoint_type="browser_remote_job_state",
                            payload_schema="provider.catalog.checkpoint",
                            from_version=1,
                            to_version=2,
                            migrate_payload=lambda payload: {**payload, "schema_version": 2, "cursor": 10},
                        ),
                    ),
                    description="Test provider checkpoint schema.",
                )
            ]
        )
        registry = CheckpointSchemaRegistry()

        catalog.apply_to_registry(registry)
        payload = normalize_task_checkpoint_payload(
            runner_name="browser_remote",
            checkpoint_type="browser_remote_job_state",
            payload={"schema": "provider.catalog.checkpoint", "schema_version": 1},
            registry=registry,
        )

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["cursor"], 10)
        self.assertEqual(catalog.payload()["entries"][0]["provider_name"], "browser-provider")


if __name__ == "__main__":
    unittest.main()
