"""Tests for OpenAI Codex token loading with per-agent storage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openheron.core.openai_codex_llm import _get_codex_token
from openheron.runtime.agent_runtime import AgentRuntimeContext, agent_runtime_context


class OpenAICodexAuthTests(unittest.TestCase):
    def test_get_codex_token_uses_agent_scoped_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp).resolve() / "agent"
            runtime = AgentRuntimeContext(
                agent_id="agent-a",
                workspace_root=Path(tmp).resolve(),
                agent_dir=agent_dir,
            )
            with agent_runtime_context(runtime):
                with patch("oauth_cli_kit.storage.FileTokenStorage") as storage_cls:
                    with patch("oauth_cli_kit.get_token") as get_token_fn:
                        token = SimpleNamespace(access="acc", account_id="acct")
                        get_token_fn.return_value = token
                        result = _get_codex_token()

        self.assertIs(result, token)
        self.assertTrue(storage_cls.called)
        kwargs = storage_cls.call_args.kwargs
        self.assertEqual(kwargs.get("import_codex_cli"), False)
        self.assertIn(str(agent_dir), str(kwargs.get("data_dir")))


if __name__ == "__main__":
    unittest.main()
