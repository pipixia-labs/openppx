"""Tests for root-agent prompt layering."""

from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from openppx.app.prompt import (
    build_root_prompt_layers,
    build_startup_runtime_context,
    build_static_policy_instruction,
)


class PromptLayeringTests(unittest.TestCase):
    def test_static_policy_excludes_startup_and_request_context(self) -> None:
        text = build_static_policy_instruction()

        self.assertIn("You are openppx", text)
        self.assertIn("Agent-home context", text)
        self.assertIn("Large task outputs may be returned as artifacts", text)
        self.assertNotIn("Runtime:", text)
        self.assertNotIn("Workspace:", text)
        self.assertNotIn("Available skills:", text)
        self.assertNotIn("Current request time:", text)

    def test_startup_context_contains_runtime_workspace_skills_and_gui_routing(self) -> None:
        registry = Mock()
        registry.build_summary.return_value = "- skill_a: test skill"
        with patch("openppx.app.prompt.get_registry", return_value=registry):
            with patch.dict(
                os.environ,
                {
                    "OPENPPX_WORKSPACE": "openppx-test-workspace",
                    "OPENPPX_MCP_SERVERS_JSON": (
                        '{"gui_remote":{"enabled":true,"command":"openppx-gui-mcp","toolNamePrefix":"desktop_"}}'
                    ),
                },
                clear=False,
            ):
                text = build_startup_runtime_context()

        self.assertIn("Runtime:", text)
        self.assertIn("Workspace: openppx-test-workspace", text)
        self.assertIn("not a user task", text)
        self.assertIn("do not acknowledge", text)
        self.assertIn("desktop_gui_task", text)
        self.assertIn("desktop_gui_action", text)
        self.assertIn("- skill_a: test skill", text)

    def test_root_prompt_renders_static_policy_before_startup_context(self) -> None:
        registry = Mock()
        registry.build_summary.return_value = "- skill_a: test skill"
        with patch("openppx.app.prompt.get_registry", return_value=registry):
            layers = build_root_prompt_layers()
            text = layers.render()

        self.assertLess(text.index("You are openppx"), text.index("# Runtime Context"))
        self.assertLess(text.index("# Runtime Context"), text.index("Available skills:"))

    def test_root_agent_uses_adk_static_instruction_for_stable_policy(self) -> None:
        from openppx import agent

        self.assertIsInstance(agent.root_agent.static_instruction, str)
        self.assertIn("You are openppx", agent.root_agent.static_instruction)
        self.assertNotIn("Workspace:", agent.root_agent.static_instruction)
        self.assertIn("# Runtime Context", agent.root_agent.instruction)
        self.assertIn("Available skills:", agent.root_agent.instruction)


if __name__ == "__main__":
    unittest.main()
