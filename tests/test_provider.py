"""Tests for provider helpers."""

from __future__ import annotations

import unittest
import os
from unittest.mock import patch

from openpipixia.core.provider import (
    build_adk_model_from_env,
    canonical_provider_name,
    normalize_model_name,
    normalize_provider_name,
    validate_provider_runtime,
)
from openpipixia.core.openai_codex_llm import OpenAICodexLlm


class ProviderTests(unittest.TestCase):
    def test_canonical_provider_name_supports_aliases(self) -> None:
        self.assertEqual(canonical_provider_name("codex"), "openai_codex")
        self.assertEqual(canonical_provider_name("copilot"), "github_copilot")
        self.assertEqual(canonical_provider_name("kimi"), "moonshot")
        self.assertEqual(canonical_provider_name("qwen"), "dashscope")

    def test_normalize_provider_name_maps_aliases(self) -> None:
        self.assertEqual(normalize_provider_name("codex"), "openai_codex")
        self.assertEqual(normalize_provider_name("openai-codex"), "openai_codex")
        self.assertEqual(normalize_provider_name("copilot"), "github_copilot")

    def test_openai_model_is_prefixed_when_missing_provider(self) -> None:
        self.assertEqual(normalize_model_name("openai", "gpt-4.1-mini"), "openai/gpt-4.1-mini")

    def test_openai_model_keeps_existing_provider_prefix(self) -> None:
        self.assertEqual(normalize_model_name("openai", "openai/gpt-4.1"), "openai/gpt-4.1")

    def test_openrouter_is_supported_by_runtime(self) -> None:
        issue = validate_provider_runtime("openrouter")
        self.assertIsNone(issue)

    def test_deepseek_model_is_prefixed_when_missing_provider(self) -> None:
        self.assertEqual(normalize_model_name("deepseek", "deepseek-chat"), "deepseek/deepseek-chat")

    def test_openai_codex_runtime_is_supported(self) -> None:
        with patch("openpipixia.core.provider.importlib.util.find_spec", return_value=object()):
            issue = validate_provider_runtime("openai_codex")
        self.assertIsNone(issue)

    def test_openai_codex_runtime_requires_oauth_cli_kit(self) -> None:
        def _find_spec(name: str):
            if name == "oauth_cli_kit":
                return None
            return object()

        with patch("openpipixia.core.provider.importlib.util.find_spec", side_effect=_find_spec):
            issue = validate_provider_runtime("openai_codex")
        self.assertIsNotNone(issue)
        self.assertIn("oauth-cli-kit", str(issue))

    def test_build_openai_codex_model_from_env(self) -> None:
        env = {
            "OPENPIPIXIA_PROVIDER": "openai_codex",
            "OPENPIPIXIA_MODEL": "openai-codex/gpt-5.1-codex",
            "OPENPIPIXIA_PROVIDER_API_BASE": "https://chatgpt.com/backend-api/codex/responses",
        }
        with patch("openpipixia.core.provider.importlib.util.find_spec", return_value=object()):
            with patch.dict(os.environ, env, clear=False):
                model = build_adk_model_from_env()
        self.assertIsInstance(model, OpenAICodexLlm)


if __name__ == "__main__":
    unittest.main()
