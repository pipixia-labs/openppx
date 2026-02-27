"""Tests for per-agent tool/system-permission guards in registry tools."""

from __future__ import annotations

import unittest
from pathlib import Path

from openheron.runtime.agent_runtime import AgentRuntimeContext, agent_runtime_context
from openheron.tooling.registry import browser, web_search


class AgentRuntimeToolPolicyTests(unittest.TestCase):
    def test_tool_deny_blocks_web_search(self) -> None:
        runtime = AgentRuntimeContext(
            agent_id="agent-a",
            workspace_root=Path(".").resolve(),
            agent_dir=Path("./.agent").resolve(),
            tools_deny=("web_search",),
        )
        with agent_runtime_context(runtime):
            output = web_search("openheron")
        self.assertIn("denied", output.lower())

    def test_system_permission_blocks_browser(self) -> None:
        runtime = AgentRuntimeContext(
            agent_id="agent-a",
            workspace_root=Path(".").resolve(),
            agent_dir=Path("./.agent").resolve(),
            system_permissions={"browser": False},
        )
        with agent_runtime_context(runtime):
            output = browser(action="status")
        self.assertIn('"status": 403', output)


if __name__ == "__main__":
    unittest.main()
