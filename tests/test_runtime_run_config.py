"""Tests for ADK RunConfig profile helpers."""

from __future__ import annotations

import unittest

from google.adk.agents.run_config import StreamingMode

from openppx.runtime.run_config import build_run_config


class RunConfigProfileTests(unittest.TestCase):
    def test_full_profile_defaults_to_non_streaming_runtime_policy(self) -> None:
        cfg = build_run_config(profile="full", custom_metadata={"channel": "local"})

        self.assertEqual(cfg.streaming_mode, StreamingMode.NONE)
        self.assertEqual(cfg.max_llm_calls, 500)
        self.assertFalse(cfg.model_dump(mode="python")["save_input_blobs_as_artifacts"])
        self.assertIsNone(cfg.get_session_config)
        self.assertEqual(cfg.custom_metadata["profile"], "full")
        self.assertEqual(cfg.custom_metadata["channel"], "local")

    def test_full_profile_can_enable_sse_streaming(self) -> None:
        cfg = build_run_config(profile="full", streaming=True)

        self.assertEqual(cfg.streaming_mode, StreamingMode.SSE)

    def test_ephemeral_profile_defaults_to_small_historyless_policy(self) -> None:
        cfg = build_run_config(profile="ephemeral", custom_metadata={"request_kind": "gui_grounding"})

        self.assertEqual(cfg.streaming_mode, StreamingMode.NONE)
        self.assertEqual(cfg.max_llm_calls, 8)
        self.assertFalse(cfg.model_dump(mode="python")["save_input_blobs_as_artifacts"])
        self.assertIsNotNone(cfg.get_session_config)
        self.assertEqual(cfg.get_session_config.num_recent_events, 0)
        self.assertEqual(cfg.custom_metadata["profile"], "ephemeral")
        self.assertEqual(cfg.custom_metadata["request_kind"], "gui_grounding")

    def test_unknown_profile_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported run config profile"):
            build_run_config(profile="unknown")


if __name__ == "__main__":
    unittest.main()
