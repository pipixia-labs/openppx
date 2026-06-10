"""Tests for runner factory compaction and memory wiring."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from google.adk.plugins.save_files_as_artifacts_plugin import SaveFilesAsArtifactsPlugin
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from openppx.runtime.adk_version import assert_supported_adk_major
from openppx.runtime.debug_callbacks import (
    OpenPpxDebugTracePlugin,
    OpenPpxProviderCompatibilityPlugin,
    OpenPpxUsageMetricsPlugin,
)
from openppx.runtime.long_task_context import LongTaskContextPlugin
from openppx.runtime.memory_ingest_plugin import OpenPpxMemoryIngestPlugin
from openppx.runtime.runner_factory import (
    _build_events_compaction_config,
    _build_events_summarizer,
    _runner_profile_policy,
    create_runner,
)
from openppx.runtime.staged_events_summarizer import OpenPpxStagedEventsSummarizer
from openppx.runtime.step_events import OpenPpxStepEventPlugin
from openppx.runtime.workspace_bootstrap import OpenPpxWorkspaceBootstrapPlugin


class RunnerFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_build_events_compaction_config_defaults(self) -> None:
        os.environ.pop("OPENPPX_COMPACTION_ENABLED", None)
        os.environ.pop("OPENPPX_COMPACTION_INTERVAL", None)
        os.environ.pop("OPENPPX_COMPACTION_OVERLAP", None)

        cfg = _build_events_compaction_config()

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.compaction_interval, 8)
        self.assertEqual(cfg.overlap_size, 1)
        self.assertIsNone(cfg.token_threshold)
        self.assertIsNone(cfg.event_retention_size)

    def test_build_events_compaction_config_allows_token_threshold_pair(self) -> None:
        os.environ["OPENPPX_COMPACTION_TOKEN_THRESHOLD"] = "12000"
        os.environ["OPENPPX_COMPACTION_EVENT_RETENTION"] = "6"

        cfg = _build_events_compaction_config()

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.token_threshold, 12000)
        self.assertEqual(cfg.event_retention_size, 6)

    def test_build_events_compaction_config_ignores_partial_token_input(self) -> None:
        os.environ["OPENPPX_COMPACTION_TOKEN_THRESHOLD"] = "12000"
        os.environ.pop("OPENPPX_COMPACTION_EVENT_RETENTION", None)

        cfg = _build_events_compaction_config()

        self.assertIsNotNone(cfg)
        self.assertIsNone(cfg.token_threshold)
        self.assertIsNone(cfg.event_retention_size)

    def test_build_events_summarizer_defaults_to_openppx_staged(self) -> None:
        fake_agent = type("FakeAgent", (), {"canonical_model": object()})()

        summarizer = _build_events_summarizer(fake_agent)

        self.assertIsInstance(summarizer, OpenPpxStagedEventsSummarizer)

    def test_build_events_summarizer_allows_adk_default(self) -> None:
        os.environ["OPENPPX_COMPACTION_SUMMARIZER"] = "adk_default"
        fake_agent = type("FakeAgent", (), {"canonical_model": object()})()

        summarizer = _build_events_summarizer(fake_agent)

        self.assertIsNone(summarizer)

    def test_build_events_summarizer_supports_strict_marker_quality_mode(self) -> None:
        os.environ["OPENPPX_COMPACTION_REQUIRE_MARKERS"] = "1"
        os.environ["OPENPPX_COMPACTION_MAX_RATIO"] = "0.7"
        os.environ["OPENPPX_COMPACTION_QUALITY_LOG_PATH"] = "/tmp/openppx-summary-quality.jsonl"
        fake_agent = type("FakeAgent", (), {"canonical_model": object()})()

        summarizer = _build_events_summarizer(fake_agent)

        self.assertIsInstance(summarizer, OpenPpxStagedEventsSummarizer)
        assert isinstance(summarizer, OpenPpxStagedEventsSummarizer)
        self.assertTrue(summarizer.require_marker_preservation)
        self.assertEqual(summarizer.max_compression_ratio, 0.7)
        self.assertEqual(summarizer.quality_log_path, "/tmp/openppx-summary-quality.jsonl")

    def test_full_profile_policy_enables_persistent_lifecycle(self) -> None:
        policy = _runner_profile_policy("full")

        self.assertTrue(policy.persistent_session)
        self.assertTrue(policy.default_memory_service)
        self.assertTrue(policy.default_artifact_service)
        self.assertTrue(policy.enable_step_events)
        self.assertTrue(policy.enable_memory_ingest)
        self.assertTrue(policy.enable_workspace_bootstrap)
        self.assertTrue(policy.enable_long_task_context)
        self.assertTrue(policy.enable_model_callbacks)
        self.assertTrue(policy.enable_input_file_artifacts)
        self.assertTrue(policy.enable_resumability)
        self.assertTrue(policy.enable_events_compaction)
        self.assertTrue(policy.enable_context_cache)

    def test_ephemeral_profile_policy_disables_persistent_lifecycle(self) -> None:
        policy = _runner_profile_policy("ephemeral")

        self.assertFalse(policy.persistent_session)
        self.assertFalse(policy.default_memory_service)
        self.assertFalse(policy.default_artifact_service)
        self.assertFalse(policy.enable_step_events)
        self.assertFalse(policy.enable_memory_ingest)
        self.assertFalse(policy.enable_workspace_bootstrap)
        self.assertFalse(policy.enable_long_task_context)
        self.assertFalse(policy.enable_model_callbacks)
        self.assertFalse(policy.enable_input_file_artifacts)
        self.assertFalse(policy.enable_resumability)
        self.assertFalse(policy.enable_events_compaction)
        self.assertFalse(policy.enable_context_cache)

    def test_create_runner_wires_memory_service_and_compaction(self) -> None:
        sentinel_memory = object()
        sentinel_artifacts = object()
        sentinel_session_service = object()
        sentinel_runner = object()
        fake_agent = object()
        sentinel_app = object()

        with patch("openppx.runtime.runner_factory.create_memory_service", return_value=sentinel_memory):
            with patch("openppx.runtime.runner_factory.create_artifact_service", return_value=sentinel_artifacts):
                with patch("openppx.runtime.runner_factory.App", return_value=sentinel_app) as mocked_app:
                    with patch("openppx.runtime.runner_factory.Runner", return_value=sentinel_runner) as mocked:
                        runner, session_service = create_runner(
                            agent=fake_agent,
                            app_name="openppx_test",
                            session_service=sentinel_session_service,
                        )

        self.assertIs(runner, sentinel_runner)
        self.assertIs(session_service, sentinel_session_service)
        mocked_app.assert_called_once()
        self.assertEqual(mocked.call_count, 1)
        kwargs = mocked.call_args.kwargs
        self.assertIs(kwargs["artifact_service"], sentinel_artifacts)
        self.assertIs(kwargs["memory_service"], sentinel_memory)
        self.assertIs(kwargs["session_service"], sentinel_session_service)
        self.assertIs(kwargs["app"], sentinel_app)
        app_kwargs = mocked_app.call_args.kwargs
        self.assertEqual(len(app_kwargs["plugins"]), 8)
        self.assertIsInstance(app_kwargs["plugins"][0], OpenPpxStepEventPlugin)
        self.assertIsInstance(app_kwargs["plugins"][1], OpenPpxMemoryIngestPlugin)
        self.assertIsInstance(app_kwargs["plugins"][2], OpenPpxWorkspaceBootstrapPlugin)
        self.assertIsInstance(app_kwargs["plugins"][3], LongTaskContextPlugin)
        self.assertIsInstance(app_kwargs["plugins"][4], OpenPpxProviderCompatibilityPlugin)
        self.assertIsInstance(app_kwargs["plugins"][5], OpenPpxUsageMetricsPlugin)
        self.assertIsInstance(app_kwargs["plugins"][6], OpenPpxDebugTracePlugin)
        self.assertIsInstance(app_kwargs["plugins"][7], SaveFilesAsArtifactsPlugin)

    def test_create_runner_full_profile_golden_assembly(self) -> None:
        sentinel_session_service = object()
        sentinel_memory_service = object()
        sentinel_artifact_service = object()
        sentinel_runner = object()
        fake_agent = object()
        sentinel_app = object()

        os.environ.pop("OPENPPX_COMPACTION_ENABLED", None)
        os.environ.pop("OPENPPX_COMPACTION_INTERVAL", None)
        os.environ.pop("OPENPPX_COMPACTION_OVERLAP", None)
        os.environ.pop("OPENPPX_COMPACTION_TOKEN_THRESHOLD", None)
        os.environ.pop("OPENPPX_COMPACTION_EVENT_RETENTION", None)

        with patch("openppx.runtime.runner_factory.App", return_value=sentinel_app) as mocked_app:
            with patch("openppx.runtime.runner_factory.Runner", return_value=sentinel_runner) as mocked_runner:
                runner, session_service = create_runner(
                    agent=fake_agent,
                    app_name="openppx",
                    session_service=sentinel_session_service,
                    memory_service=sentinel_memory_service,
                    artifact_service=sentinel_artifact_service,
                )

        self.assertIs(runner, sentinel_runner)
        self.assertIs(session_service, sentinel_session_service)

        app_kwargs = mocked_app.call_args.kwargs
        self.assertEqual(app_kwargs["name"], "openppx")
        self.assertIs(app_kwargs["root_agent"], fake_agent)
        self.assertTrue(app_kwargs["resumability_config"].is_resumable)
        self.assertEqual(app_kwargs["events_compaction_config"].compaction_interval, 8)
        self.assertEqual(app_kwargs["events_compaction_config"].overlap_size, 1)
        self.assertIsNone(app_kwargs["events_compaction_config"].token_threshold)
        self.assertIsNone(app_kwargs["events_compaction_config"].event_retention_size)
        self.assertIsNone(app_kwargs.get("context_cache_config"))
        self.assertEqual(
            [type(plugin) for plugin in app_kwargs["plugins"]],
            [
                OpenPpxStepEventPlugin,
                OpenPpxMemoryIngestPlugin,
                OpenPpxWorkspaceBootstrapPlugin,
                LongTaskContextPlugin,
                OpenPpxProviderCompatibilityPlugin,
                OpenPpxUsageMetricsPlugin,
                OpenPpxDebugTracePlugin,
                SaveFilesAsArtifactsPlugin,
            ],
        )

        runner_kwargs = mocked_runner.call_args.kwargs
        self.assertIs(runner_kwargs["app"], sentinel_app)
        self.assertEqual(runner_kwargs["app_name"], "openppx")
        self.assertIs(runner_kwargs["session_service"], sentinel_session_service)
        self.assertIs(runner_kwargs["memory_service"], sentinel_memory_service)
        self.assertIs(runner_kwargs["artifact_service"], sentinel_artifact_service)
        self.assertTrue(runner_kwargs["auto_create_session"])

    def test_create_runner_accepts_explicit_full_profile(self) -> None:
        sentinel_session_service = object()
        sentinel_memory_service = object()
        sentinel_artifact_service = object()
        sentinel_runner = object()
        fake_agent = object()
        sentinel_app = object()

        with patch("openppx.runtime.runner_factory.App", return_value=sentinel_app) as mocked_app:
            with patch("openppx.runtime.runner_factory.Runner", return_value=sentinel_runner) as mocked_runner:
                runner, session_service = create_runner(
                    agent=fake_agent,
                    app_name="openppx",
                    profile="full",
                    session_service=sentinel_session_service,
                    memory_service=sentinel_memory_service,
                    artifact_service=sentinel_artifact_service,
                )

        self.assertIs(runner, sentinel_runner)
        self.assertIs(session_service, sentinel_session_service)
        self.assertEqual(mocked_app.call_args.kwargs["name"], "openppx")
        self.assertIs(mocked_runner.call_args.kwargs["app"], sentinel_app)

    def test_create_runner_ephemeral_profile_uses_minimal_assembly(self) -> None:
        sentinel_runner = object()
        fake_agent = object()
        sentinel_app = object()

        with patch("openppx.runtime.runner_factory.create_session_service") as mocked_session:
            with patch("openppx.runtime.runner_factory.create_memory_service") as mocked_memory:
                with patch("openppx.runtime.runner_factory.create_artifact_service") as mocked_artifacts:
                    with patch("openppx.runtime.runner_factory.App", return_value=sentinel_app) as mocked_app:
                        with patch("openppx.runtime.runner_factory.Runner", return_value=sentinel_runner) as mocked:
                            runner, session_service = create_runner(
                                agent=fake_agent,
                                app_name="openppx_gui_planner",
                                profile="ephemeral",
                            )

        self.assertIs(runner, sentinel_runner)
        self.assertIsInstance(session_service, InMemorySessionService)
        mocked_session.assert_not_called()
        mocked_memory.assert_not_called()
        mocked_artifacts.assert_not_called()

        app_kwargs = mocked_app.call_args.kwargs
        self.assertEqual(app_kwargs["name"], "openppx_gui_planner")
        self.assertIs(app_kwargs["root_agent"], fake_agent)
        self.assertEqual(app_kwargs["plugins"], [])
        self.assertIsNone(app_kwargs.get("resumability_config"))
        self.assertIsNone(app_kwargs.get("events_compaction_config"))
        self.assertIsNone(app_kwargs.get("context_cache_config"))

        runner_kwargs = mocked.call_args.kwargs
        self.assertIs(runner_kwargs["app"], sentinel_app)
        self.assertEqual(runner_kwargs["app_name"], "openppx_gui_planner")
        self.assertIs(runner_kwargs["session_service"], session_service)
        self.assertIsNone(runner_kwargs["memory_service"])
        self.assertIsNone(runner_kwargs["artifact_service"])
        self.assertTrue(runner_kwargs["auto_create_session"])

    def test_create_runner_rejects_unknown_profile_before_creating_services(self) -> None:
        with patch("openppx.runtime.runner_factory.create_session_service") as mocked_session:
            with self.assertRaisesRegex(ValueError, "unsupported runner profile"):
                create_runner(agent=object(), app_name="openppx", profile="unknown")

        mocked_session.assert_not_called()

    def test_create_runner_wires_context_cache_when_requested(self) -> None:
        sentinel_session_service = object()
        sentinel_memory_service = object()
        sentinel_artifact_service = object()
        sentinel_runner = object()
        fake_agent = object()
        sentinel_app = object()

        with patch.dict(os.environ, {"OPENPPX_CONTEXT_CACHE_ENABLED": "1"}, clear=False):
            with patch("openppx.runtime.runner_factory.App", return_value=sentinel_app) as mocked_app:
                with patch("openppx.runtime.runner_factory.Runner", return_value=sentinel_runner):
                    runner, session_service = create_runner(
                        agent=fake_agent,
                        app_name="openppx",
                        session_service=sentinel_session_service,
                        memory_service=sentinel_memory_service,
                        artifact_service=sentinel_artifact_service,
                    )

        self.assertIs(runner, sentinel_runner)
        self.assertIs(session_service, sentinel_session_service)
        config = mocked_app.call_args.kwargs["context_cache_config"]
        self.assertIsNotNone(config)
        self.assertEqual(config.cache_intervals, 5)
        self.assertEqual(config.min_tokens, 4096)
        self.assertEqual(config.ttl_seconds, 600)

    def test_adk_major_version_guard_allows_adk_2(self) -> None:
        with patch("openppx.runtime.adk_version.installed_adk_version", return_value="2.1.0"):
            assert_supported_adk_major()

    def test_adk_major_version_guard_rejects_adk_1(self) -> None:
        with patch("openppx.runtime.adk_version.installed_adk_version", return_value="1.31.0"):
            with self.assertRaisesRegex(RuntimeError, "requires google-adk 2.x"):
                assert_supported_adk_major()


if __name__ == "__main__":
    unittest.main()
