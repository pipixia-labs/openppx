"""Tests for per-agent runtime overrides in security policy loading."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from openheron.core.security import load_security_policy
from openheron.runtime.agent_runtime import AgentRuntimeContext, agent_runtime_context


class AgentRuntimeSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_runtime_context_overrides_env_security(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            os.environ["OPENHERON_WORKSPACE"] = "/tmp/wrong"
            os.environ["OPENHERON_ALLOW_EXEC"] = "1"
            os.environ["OPENHERON_ALLOW_NETWORK"] = "1"
            os.environ["OPENHERON_RESTRICT_TO_WORKSPACE"] = "0"

            runtime = AgentRuntimeContext(
                agent_id="agent-a",
                workspace_root=workspace,
                agent_dir=workspace / "agent",
                allow_exec=False,
                allow_network=False,
                restrict_to_workspace=True,
                exec_allowlist=("python",),
            )
            with agent_runtime_context(runtime):
                policy = load_security_policy()

        self.assertEqual(policy.workspace_root, workspace)
        self.assertFalse(policy.allow_exec)
        self.assertFalse(policy.allow_network)
        self.assertTrue(policy.restrict_to_workspace)
        self.assertEqual(policy.exec_allowlist, ("python",))


if __name__ == "__main__":
    unittest.main()
