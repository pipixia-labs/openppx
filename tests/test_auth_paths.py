"""Tests for per-agent auth storage path helpers."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from openheron.core.auth_paths import (
    resolve_current_agent_dir,
    resolve_github_copilot_token_dir,
    resolve_openai_codex_oauth_data_dir,
)
from openheron.runtime.agent_runtime import AgentRuntimeContext, agent_runtime_context


class AuthPathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_resolve_current_agent_dir_prefers_runtime_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp).resolve() / "agent-runtime"
            runtime = AgentRuntimeContext(
                agent_id="agent-a",
                workspace_root=Path(tmp).resolve(),
                agent_dir=runtime_dir,
            )
            os.environ["OPENHERON_AGENT_DIR"] = "/tmp/wrong"
            with agent_runtime_context(runtime):
                resolved = resolve_current_agent_dir()
        self.assertEqual(resolved, runtime_dir)

    def test_resolve_current_agent_dir_falls_back_to_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp).resolve() / "agent-env"
            os.environ["OPENHERON_AGENT_DIR"] = str(expected)
            self.assertEqual(resolve_current_agent_dir(), expected)

    def test_auth_child_paths_are_under_agent_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp).resolve() / "agent"
            codex_data = resolve_openai_codex_oauth_data_dir(agent_dir)
            copilot_dir = resolve_github_copilot_token_dir(agent_dir)

        self.assertTrue(str(codex_data).startswith(str(agent_dir)))
        self.assertTrue(str(copilot_dir).startswith(str(agent_dir)))


if __name__ == "__main__":
    unittest.main()
