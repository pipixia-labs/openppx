"""Tests for agent bootstrap prompt injection."""

from __future__ import annotations

import asyncio
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from google.genai import types as genai_types

from openppx.runtime.workspace_bootstrap import (
    before_model_workspace_bootstrap_callback,
    load_workspace_bootstrap_sections,
)


class WorkspaceBootstrapTests(unittest.TestCase):
    def test_loader_reads_openclaw_style_files_in_fixed_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("agents-rules", encoding="utf-8")
            (root / "SOUL.md").write_text("soul-tone", encoding="utf-8")
            (root / "TOOLS.md").write_text("tool-usage-notes", encoding="utf-8")
            (root / "IDENTITY.md").write_text("identity-profile", encoding="utf-8")
            (root / "USER.md").write_text("user-profile", encoding="utf-8")

            sections = load_workspace_bootstrap_sections(root)

        self.assertEqual(
            [item.name for item in sections],
            ["AGENTS.md", "SOUL.md", "TOOLS.md", "IDENTITY.md", "USER.md"],
        )
        merged = "\n".join(item.content for item in sections)
        self.assertIn("agents-rules", merged)
        self.assertIn("soul-tone", merged)
        self.assertIn("tool-usage-notes", merged)
        self.assertIn("identity-profile", merged)
        self.assertIn("user-profile", merged)

    def test_callback_inserts_workspace_context_as_request_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("follow local agent rules", encoding="utf-8")
            (root / "SOUL.md").write_text("keep a concise tone", encoding="utf-8")
            (root / "TOOLS.md").write_text("always check tool constraints first", encoding="utf-8")
            (root / "IDENTITY.md").write_text("name: openppx", encoding="utf-8")
            (root / "USER.md").write_text("user prefers chinese", encoding="utf-8")

            llm_request = types.SimpleNamespace(
                config=types.SimpleNamespace(system_instruction="base-system-instruction"),
                contents=[
                    genai_types.Content(
                        role="user",
                        parts=[genai_types.Part.from_text(text="current user request")],
                    )
                ],
            )

            with patch.dict(os.environ, {"OPENPPX_AGENT_HOME": str(root)}, clear=False):
                asyncio.run(before_model_workspace_bootstrap_callback(types.SimpleNamespace(), llm_request))

        self.assertEqual(llm_request.config.system_instruction, "base-system-instruction")
        self.assertEqual(len(llm_request.contents), 2)
        bootstrap_text = "".join(part.text or "" for part in llm_request.contents[0].parts)
        current_text = "".join(part.text or "" for part in llm_request.contents[1].parts)
        self.assertIn("Agent Context (injected by openppx)", bootstrap_text)
        self.assertIn("## AGENTS.md", bootstrap_text)
        self.assertIn("## SOUL.md", bootstrap_text)
        self.assertIn("## TOOLS.md", bootstrap_text)
        self.assertIn("## IDENTITY.md", bootstrap_text)
        self.assertIn("## USER.md", bootstrap_text)
        self.assertIn("follow local agent rules", bootstrap_text)
        self.assertIn("keep a concise tone", bootstrap_text)
        self.assertIn("always check tool constraints first", bootstrap_text)
        self.assertIn("name: openppx", bootstrap_text)
        self.assertIn("user prefers chinese", bootstrap_text)
        self.assertEqual(current_text, "current user request")

    def test_callback_keeps_instruction_when_no_supported_files_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            llm_request = types.SimpleNamespace(
                config=types.SimpleNamespace(system_instruction="base-system-instruction"),
                contents=[],
            )
            with patch.dict(os.environ, {"OPENPPX_AGENT_HOME": str(root)}, clear=False):
                asyncio.run(before_model_workspace_bootstrap_callback(types.SimpleNamespace(), llm_request))

        self.assertEqual(llm_request.config.system_instruction, "base-system-instruction")
        self.assertEqual(llm_request.contents, [])

    def test_callback_accepts_adk_keyword_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("agents-rules", encoding="utf-8")
            llm_request = types.SimpleNamespace(
                config=types.SimpleNamespace(system_instruction="base-system-instruction"),
                contents=[],
            )
            with patch.dict(os.environ, {"OPENPPX_AGENT_HOME": str(root)}, clear=False):
                asyncio.run(
                    before_model_workspace_bootstrap_callback(
                        callback_context=types.SimpleNamespace(),
                        llm_request=llm_request,
                    )
                )

        bootstrap_text = "".join(part.text or "" for part in llm_request.contents[0].parts)
        self.assertIn("Agent Context (injected by openppx)", bootstrap_text)

    def test_callback_does_not_duplicate_list_system_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("agents-rules", encoding="utf-8")
            llm_request = types.SimpleNamespace(
                config=types.SimpleNamespace(
                    system_instruction=[
                        "base-system-instruction",
                        "# Agent Context (injected by openppx)\n\n## AGENTS.md\n\nagents-rules",
                    ]
                ),
                contents=[],
            )
            with patch.dict(os.environ, {"OPENPPX_AGENT_HOME": str(root)}, clear=False):
                asyncio.run(before_model_workspace_bootstrap_callback(types.SimpleNamespace(), llm_request))

        self.assertEqual(len(llm_request.config.system_instruction), 2)
        self.assertEqual(llm_request.contents, [])

    def test_callback_does_not_duplicate_existing_content_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("agents-rules", encoding="utf-8")
            llm_request = types.SimpleNamespace(
                config=types.SimpleNamespace(system_instruction="base-system-instruction"),
                contents=[
                    genai_types.Content(
                        role="user",
                        parts=[
                            genai_types.Part.from_text(
                                text="# Agent Context (injected by openppx)\n\n## AGENTS.md\n\nagents-rules"
                            )
                        ],
                    ),
                    genai_types.Content(
                        role="user",
                        parts=[genai_types.Part.from_text(text="current user request")],
                    ),
                ],
            )
            with patch.dict(os.environ, {"OPENPPX_AGENT_HOME": str(root)}, clear=False):
                asyncio.run(before_model_workspace_bootstrap_callback(types.SimpleNamespace(), llm_request))

        self.assertEqual(len(llm_request.contents), 2)

    def test_callback_inserts_after_prior_model_content_and_before_latest_user_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("agents-rules", encoding="utf-8")
            llm_request = types.SimpleNamespace(
                config=types.SimpleNamespace(system_instruction="base-system-instruction"),
                contents=[
                    genai_types.Content(
                        role="model",
                        parts=[genai_types.Part.from_text(text="previous model response")],
                    ),
                    genai_types.Content(
                        role="user",
                        parts=[genai_types.Part.from_text(text="current user request")],
                    ),
                ],
            )
            with patch.dict(os.environ, {"OPENPPX_AGENT_HOME": str(root)}, clear=False):
                asyncio.run(before_model_workspace_bootstrap_callback(types.SimpleNamespace(), llm_request))

        self.assertEqual([content.role for content in llm_request.contents], ["model", "user", "user"])
        bootstrap_text = "".join(part.text or "" for part in llm_request.contents[1].parts)
        self.assertIn("Agent Context (injected by openppx)", bootstrap_text)


if __name__ == "__main__":
    unittest.main()
