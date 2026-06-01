"""Tests for context-cache safety guardrails."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from openppx.runtime.context_cache import build_context_cache_config, context_cache_requested


class ContextCacheGuardTests(unittest.TestCase):
    def test_context_cache_is_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(context_cache_requested())
            self.assertIsNone(build_context_cache_config(profile="full"))

    def test_context_cache_disabled_values_are_false(self) -> None:
        for raw in ("0", "false", "off", "no", ""):
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {"OPENPPX_CONTEXT_CACHE_ENABLED": raw}, clear=True):
                    self.assertFalse(context_cache_requested())
                    self.assertIsNone(build_context_cache_config(profile="full"))

    def test_ephemeral_profile_never_builds_context_cache_config(self) -> None:
        with patch.dict(os.environ, {"OPENPPX_CONTEXT_CACHE_ENABLED": "1"}, clear=True):
            self.assertIsNone(build_context_cache_config(profile="ephemeral"))

    def test_full_profile_context_cache_request_builds_config(self) -> None:
        with patch.dict(os.environ, {"OPENPPX_CONTEXT_CACHE_ENABLED": "1"}, clear=True):
            config = build_context_cache_config(profile="full")

        self.assertIsNotNone(config)
        self.assertEqual(config.cache_intervals, 5)
        self.assertEqual(config.min_tokens, 4096)
        self.assertEqual(config.ttl_seconds, 600)

    def test_full_profile_context_cache_accepts_bounded_env_overrides(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENPPX_CONTEXT_CACHE_ENABLED": "1",
                "OPENPPX_CONTEXT_CACHE_INTERVALS": "3",
                "OPENPPX_CONTEXT_CACHE_MIN_TOKENS": "8192",
                "OPENPPX_CONTEXT_CACHE_TTL_SECONDS": "1200",
            },
            clear=True,
        ):
            config = build_context_cache_config(profile="full")

        self.assertIsNotNone(config)
        self.assertEqual(config.cache_intervals, 3)
        self.assertEqual(config.min_tokens, 8192)
        self.assertEqual(config.ttl_seconds, 1200)


if __name__ == "__main__":
    unittest.main()
