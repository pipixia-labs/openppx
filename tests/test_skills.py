"""Tests for ppx skills behavior."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from openpipixia.tooling.skills_adapter import SkillRegistry, list_skills, read_skill


class SkillRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_discovers_builtin_skill(self) -> None:
        registry = SkillRegistry(workspace=Path("/tmp/nonexistent-openpipixia-workspace"))
        names = [s.name for s in registry.list_skills()]
        self.assertIn("general", names)

    def test_discovers_agent_home_skills_via_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_home = Path(tmp)
            skill_dir = agent_home / "skills" / "workspace-demo"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: workspace-demo\ndescription: workspace skill\n---\n\n# Workspace Demo\n",
                encoding="utf-8",
            )

            os.environ["OPENPIPIXIA_AGENT_HOME"] = str(agent_home)
            skills = json.loads(list_skills())
            names = {item["name"] for item in skills}
            self.assertIn("workspace-demo", names)

    def test_builtin_skill_wins_when_workspace_name_collides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            skill_dir = workspace / "skills" / "general"
            skill_dir.mkdir(parents=True, exist_ok=True)
            custom = (
                "---\n"
                "name: general\n"
                "description: custom general\n"
                "---\n\n"
                "# Custom General\n"
            )
            (skill_dir / "SKILL.md").write_text(custom, encoding="utf-8")

            registry = SkillRegistry(workspace=workspace)
            skills = {item.name: item for item in registry.list_skills()}
            self.assertEqual(skills["general"].source, "builtin")
            self.assertIn("# General Skill", registry.read_skill("general"))
            self.assertNotIn("# Custom General", registry.read_skill("general"))

    def test_read_skill_raises_for_missing(self) -> None:
        registry = SkillRegistry(workspace=Path("/tmp/nonexistent-openpipixia-workspace"))
        with self.assertRaises(ValueError):
            registry.read_skill("does-not-exist")

    def test_summary_escapes_xml_sensitive_chars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            skill_dir = workspace / "skills" / "escape-demo"
            skill_dir.mkdir(parents=True, exist_ok=True)
            content = (
                "---\n"
                "name: escape-demo\n"
                "description: A&B<C>\n"
                "---\n\n"
                "# Escape Demo\n"
            )
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

            registry = SkillRegistry(workspace=workspace)
            summary = registry.build_summary()
            self.assertIn("A&amp;B&lt;C&gt;", summary)

    def test_read_skill_tool_uses_env_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            skill_dir = workspace / "skills" / "demo"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: demo\ndescription: demo\n---\n\n# Demo Skill\n",
                encoding="utf-8",
            )

            os.environ["OPENPIPIXIA_AGENT_HOME"] = str(workspace)
            content = read_skill("demo")
            self.assertIn("# Demo Skill", content)

    def test_discovers_skills_via_builtin_override_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            builtin_dir = Path(tmp) / "custom_builtin"
            skill_dir = builtin_dir / "custom-skill"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: custom-skill\ndescription: custom builtin\n---\n\n# Custom Builtin\n",
                encoding="utf-8",
            )
            os.environ["OPENPIPIXIA_AGENT_HOME"] = "/tmp/nonexistent-openpipixia-agent-home"
            os.environ["OPENPIPIXIA_BUILTIN_SKILLS_DIR"] = str(builtin_dir)

            skills = json.loads(list_skills())
            names = {item["name"] for item in skills}
            self.assertIn("custom-skill", names)

    def test_builtin_contains_all_expected_skills(self) -> None:
        registry = SkillRegistry(workspace=Path("/tmp/nonexistent-openpipixia-workspace"))
        names = {item.name for item in registry.list_skills()}
        expected = {
            "cron",
            "docx",
            "github",
            "memory",
            "planning-with-files",
            "pptx",
            "skill-creator",
            "summarize",
            "tmux",
            "weather",
            "xlsx",
        }
        self.assertTrue(expected.issubset(names))

    def test_default_user_global_skills_use_openpipixia_dir_not_codex_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            openpipixia_skill = home / ".openpipixia" / "skills" / "openpipixia-global-demo" / "SKILL.md"
            openpipixia_skill.parent.mkdir(parents=True, exist_ok=True)
            openpipixia_skill.write_text(
                "---\nname: openpipixia-global-demo\ndescription: openpipixia global skill\n---\n\n# Openpipixia Global\n",
                encoding="utf-8",
            )
            codex_skill = home / ".codex" / "skills" / "codex-global-demo" / "SKILL.md"
            codex_skill.parent.mkdir(parents=True, exist_ok=True)
            codex_skill.write_text(
                "---\nname: codex-global-demo\ndescription: codex global skill\n---\n\n# Codex Global\n",
                encoding="utf-8",
            )

            os.environ["HOME"] = str(home)
            registry = SkillRegistry(workspace=Path("/tmp/nonexistent-openpipixia-workspace"))
            names = {item.name for item in registry.list_skills()}

        self.assertIn("openpipixia-global-demo", names)
        self.assertNotIn("codex-global-demo", names)


if __name__ == "__main__":
    unittest.main()
