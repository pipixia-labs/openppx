"""Tests for agent runtime environment export behavior."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from openheron.runtime.agent_runtime import AgentRuntimeContext, agent_runtime_context


class AgentRuntimeEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_context_exports_and_restores_agent_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp).resolve() / "agent"
            runtime = AgentRuntimeContext(
                agent_id="agent-a",
                workspace_root=Path(tmp).resolve(),
                agent_dir=agent_dir,
            )
            os.environ["OPENHERON_AGENT_DIR"] = "/tmp/original-agent"
            os.environ["GITHUB_COPILOT_TOKEN_DIR"] = "/tmp/original-copilot"

            with agent_runtime_context(runtime):
                self.assertEqual(os.environ.get("OPENHERON_AGENT_DIR"), str(agent_dir))
                self.assertEqual(
                    os.environ.get("GITHUB_COPILOT_TOKEN_DIR"),
                    str(agent_dir / "auth" / "github_copilot"),
                )

            self.assertEqual(os.environ.get("OPENHERON_AGENT_DIR"), "/tmp/original-agent")
            self.assertEqual(os.environ.get("GITHUB_COPILOT_TOKEN_DIR"), "/tmp/original-copilot")


if __name__ == "__main__":
    unittest.main()
