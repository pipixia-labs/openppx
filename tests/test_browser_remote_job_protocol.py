"""Tests for browser remote job protocol parsing."""

from __future__ import annotations

import unittest

from openppx.runtime.browser_remote_job_protocol import (
    browser_remote_job_protocol_from_payload,
    normalize_browser_remote_job_checkpoint_payload,
)
from openppx.runtime.checkpoint_schema import (
    CheckpointMigrationSpec,
    CheckpointSchemaRegistry,
    CheckpointSchemaSpec,
    TASK_CHECKPOINT_METADATA_KEY,
)
from openppx.runtime.checkpoint_migration_catalog import (
    OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA,
    OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION,
)


class BrowserRemoteJobProtocolTests(unittest.TestCase):
    def test_checkpoint_only_protocol_is_valid(self) -> None:
        protocol = browser_remote_job_protocol_from_payload(
            {
                "checkpointPath": "state.checkpoint",
                "checkpointSchema": "browser.remote.checkpoint",
                "checkpointSchemaVersion": 2,
            }
        )

        self.assertIsNotNone(protocol)
        assert protocol is not None
        self.assertEqual(protocol.checkpoint_path, "state.checkpoint")
        self.assertEqual(protocol.checkpoint_schema, "browser.remote.checkpoint")
        self.assertEqual(protocol.checkpoint_schema_version, 2)
        self.assertTrue(protocol.runner_capabilities["checkpoint"])
        self.assertFalse(protocol.runner_capabilities["resume"])

    def test_empty_protocol_is_ignored(self) -> None:
        self.assertIsNone(browser_remote_job_protocol_from_payload({"enabled": True}))

    def test_old_checkpoint_version_uses_registered_migration(self) -> None:
        protocol = browser_remote_job_protocol_from_payload(
            {
                "checkpointPath": "checkpoint",
                "checkpointSchema": "browser.remote.migrated",
                "checkpointSchemaVersion": 2,
            }
        )
        assert protocol is not None
        registry = CheckpointSchemaRegistry(
            specs=[
                CheckpointSchemaSpec(
                    runner_name="browser_remote",
                    checkpoint_type="browser_remote_job_state",
                    payload_schema="browser.remote.migrated",
                    payload_schema_version=2,
                    normalize_payload=lambda payload: payload,
                )
            ],
            migrations=[
                CheckpointMigrationSpec(
                    runner_name="browser_remote",
                    checkpoint_type="browser_remote_job_state",
                    payload_schema="browser.remote.migrated",
                    from_version=1,
                    to_version=2,
                    migrate_payload=lambda payload: {**payload, "schema_version": 2, "cursor": 2},
                )
            ],
        )

        payload = normalize_browser_remote_job_checkpoint_payload(
            protocol=protocol,
            payload={"schema": "browser.remote.migrated", "schema_version": 1},
            registry=registry,
        )

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["cursor"], 2)
        self.assertEqual(payload[TASK_CHECKPOINT_METADATA_KEY]["migration_path"], ["1->2"])

    def test_openppx_default_browser_checkpoint_schema_migrates_without_manual_registration(self) -> None:
        protocol = browser_remote_job_protocol_from_payload(
            {
                "checkpointPath": "checkpoint",
                "checkpointSchema": OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA,
                "checkpointSchemaVersion": OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION,
            }
        )
        assert protocol is not None

        payload = normalize_browser_remote_job_checkpoint_payload(
            protocol=protocol,
            payload={
                "schema": OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA,
                "schemaVersion": 1,
                "jobId": "remote-job-1",
                "pageUrl": "https://example.test",
            },
        )

        self.assertEqual(payload["schema_version"], OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION)
        self.assertEqual(payload["job_id"], "remote-job-1")
        self.assertEqual(payload["current_url"], "https://example.test")
        self.assertEqual(payload["output_offset"], 0)
        self.assertEqual(payload[TASK_CHECKPOINT_METADATA_KEY]["migration_path"], ["1->2"])


if __name__ == "__main__":
    unittest.main()
