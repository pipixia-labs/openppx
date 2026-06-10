"""Tests for browser remote provider contract harness."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from openppx.runtime.browser_remote_contract import run_browser_remote_job_contract
from openppx.runtime.checkpoint_migration_catalog import OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION
from openppx.tooling.registry import check_browser_remote_job_protocol
from tests.support.browser_remote_provider_fixture import BrowserRemoteProviderFixture


class _DummyResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "_DummyResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class BrowserRemoteContractTests(unittest.TestCase):
    def test_contract_harness_checks_read_paths_and_skips_controls_by_default(self) -> None:
        requests: list[tuple[str, str]] = []

        def _urlopen(req, timeout):
            requests.append((req.get_method(), req.full_url))
            if req.full_url.endswith("/output?job_id=job-1"):
                return _DummyResponse({"output": "remote output"})
            return _DummyResponse({"status": "running", "summary": "remote running"})

        with mock.patch("openppx.runtime.browser_remote_job_protocol.urlopen", side_effect=_urlopen):
            report = run_browser_remote_job_contract(
                proxy_url="http://proxy.local",
                job_id="job-1",
                protocol_payload={
                    "statusPath": "/jobs/{job_id}",
                    "outputPath": "/output",
                    "pausePath": "/jobs/{job_id}/pause",
                },
            )

        payload = report.to_payload()
        self.assertTrue(payload["ok"])
        self.assertIn(("GET", "http://proxy.local/jobs/job-1"), requests)
        self.assertIn(("GET", "http://proxy.local/output?job_id=job-1"), requests)
        pause_step = next(step for step in payload["steps"] if step["name"] == "pause")
        self.assertTrue(pause_step["skipped"])

    def test_contract_tool_can_run_side_effecting_controls_when_explicit(self) -> None:
        requests: list[tuple[str, str]] = []

        def _urlopen(req, timeout):
            requests.append((req.get_method(), req.full_url))
            return _DummyResponse({"status": "paused", "summary": "ok"})

        with mock.patch("openppx.runtime.browser_remote_job_protocol.urlopen", side_effect=_urlopen):
            payload = json.loads(
                check_browser_remote_job_protocol(
                    proxy_url="http://proxy.local",
                    job_id="job-1",
                    job_protocol={"pausePath": "/jobs/{job_id}/pause"},
                    include_control_steps=True,
                )
            )

        self.assertTrue(payload["ok"])
        self.assertIn(("POST", "http://proxy.local/jobs/job-1/pause"), requests)

    def test_contract_harness_runs_against_local_http_provider_fixture(self) -> None:
        provider = BrowserRemoteProviderFixture()
        try:
            provider.start()
        except PermissionError as exc:
            self.skipTest(f"local HTTP fixture cannot bind in this environment: {exc}")
        try:
            report = run_browser_remote_job_contract(
                proxy_url=provider.proxy_url,
                job_id=provider.job.job_id,
                protocol_payload=provider.protocol_payload,
                token=provider.token,
                include_control_steps=True,
                checkpoint_payload=provider.legacy_checkpoint_payload,
            )
        finally:
            provider.stop()

        payload = report.to_payload()
        self.assertTrue(payload["ok"])
        step_payloads = {step["name"]: step for step in payload["steps"]}
        self.assertEqual(
            step_payloads["checkpoint"]["payload"]["schema_version"],
            OPENPPX_BROWSER_REMOTE_JOB_CHECKPOINT_SCHEMA_VERSION,
        )
        self.assertEqual(step_payloads["checkpoint"]["payload"]["job_id"], "fixture-job-1")
        self.assertEqual(step_payloads["checkpoint"]["payload"]["current_url"], "https://example.test/fixture")
        self.assertEqual([call["path"] for call in provider.job.calls], [
            "/jobs/fixture-job-1",
            "/jobs/fixture-job-1/output",
            "/jobs/fixture-job-1/pause",
            "/jobs/fixture-job-1/resume",
            "/jobs/fixture-job-1/cancel",
        ])


if __name__ == "__main__":
    unittest.main()
