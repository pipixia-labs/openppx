"""Skills discovery and loading for openheron."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from ..core.env_utils import env_enabled

_SKILL_NAME_ALIASES: dict[str, str] = {
    "self-observe": "memory",
}


@dataclass(frozen=True)
class SkillInfo:
    """Metadata for one discovered skill."""

    name: str
    path: Path
    source: str
    description: str


class SkillRegistry:
    """Discover skills from workspace and bundled directories."""

    def __init__(self, workspace: Path | None = None, builtin_skills_dir: Path | None = None):
        cwd = Path.cwd() if workspace is None else workspace
        self.workspace = cwd.resolve()
        self.workspace_skills_dir = self.workspace / "skills"
        package_builtin_dir = Path(__file__).resolve().parent.parent / "skills"
        if builtin_skills_dir is not None:
            self.builtin_skills_dirs = [builtin_skills_dir.resolve()]
        else:
            dirs: list[Path] = [package_builtin_dir.resolve()]
            openheron_builtin_dir = Path.home() / ".openheron" / "skills"
            if openheron_builtin_dir.resolve() not in dirs:
                dirs.append(openheron_builtin_dir.resolve())
            self.builtin_skills_dirs = dirs

    def list_skills(self) -> list[SkillInfo]:
        """List workspace + bundled skills, builtin taking precedence on name collisions."""
        discovered: dict[str, SkillInfo] = {}

        for builtin_dir in self.builtin_skills_dirs:
            for info in self._scan(builtin_dir, source="builtin"):
                discovered[info.name] = info

        for info in self._scan(self.workspace_skills_dir, source="workspace"):
            if info.name in discovered:
                # Workspace skills are not allowed to shadow bundled skills.
                # Keep bundled behavior deterministic and ignore conflicting local copies.
                logger.warning(
                    "Ignoring workspace skill '{}' at {} because a builtin skill with the same name exists.",
                    info.name,
                    info.path,
                )
                continue
            discovered[info.name] = info

        items = sorted(discovered.values(), key=lambda item: item.name.lower())
        if _debug_enabled():
            logger.debug(
                "[DEBUG] {}: {}",
                "skills.list",
                _debug_body(
                    {
                        "workspace": str(self.workspace),
                        "workspace_skills_dir": str(self.workspace_skills_dir),
                        "builtin_skills_dirs": [str(p) for p in self.builtin_skills_dirs],
                        "count": len(items),
                        "names": [i.name for i in items],
                    }
                ),
            )
        return items

    def read_skill(self, name: str) -> str:
        """Read full SKILL.md content by skill name."""
        key = name.strip()
        if not key:
            raise ValueError("Skill name cannot be empty.")

        for info in self.list_skills():
            if info.name == key:
                if _debug_enabled():
                    logger.debug(
                        "[DEBUG] {}: {}",
                        "skills.read",
                        _debug_body({"name": key, "source": info.source, "path": str(info.path)}),
                    )
                return info.path.read_text(encoding="utf-8")
        if _debug_enabled():
            logger.debug("[DEBUG] {}: {}", "skills.read.miss", _debug_body({"name": key}))
        raise ValueError(f"Skill '{key}' not found.")

    def build_summary(self) -> str:
        """Create an XML-like summary for the system prompt."""
        lines = ["<skills>"]
        for info in self.list_skills():
            lines.append("  <skill>")
            lines.append(f"    <name>{_xml_escape(info.name)}</name>")
            lines.append(f"    <description>{_xml_escape(info.description)}</description>")
            lines.append(f"    <source>{info.source}</source>")
            lines.append(f"    <location>{_xml_escape(str(info.path))}</location>")
            lines.append("  </skill>")
        lines.append("</skills>")
        return "\n".join(lines)

    def _scan(self, skills_dir: Path, source: str) -> list[SkillInfo]:
        if not skills_dir.exists() or not skills_dir.is_dir():
            return []

        result: list[SkillInfo] = []
        for child in skills_dir.iterdir():
            if not child.is_dir():
                continue
            skill_file = child / "SKILL.md"
            if not skill_file.is_file():
                continue
            description = self._extract_description(skill_file)
            result.append(
                SkillInfo(
                    name=child.name,
                    path=skill_file.resolve(),
                    source=source,
                    description=description or child.name,
                )
            )
            alias = _SKILL_NAME_ALIASES.get(child.name)
            if alias:
                result.append(
                    SkillInfo(
                        name=alias,
                        path=skill_file.resolve(),
                        source=source,
                        description=description or alias,
                    )
                )
        return result

    def _extract_description(self, skill_file: Path) -> str | None:
        content = skill_file.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return None
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return None

        for line in match.group(1).split("\n"):
            if not line.strip().startswith("description:"):
                continue
            _, value = line.split(":", 1)
            return value.strip().strip("\"'")
        return None


def get_registry() -> SkillRegistry:
    """Build registry from configured workspace or current directory."""
    workspace_env = os.getenv("OPENHERON_WORKSPACE")
    workspace = Path(workspace_env).expanduser() if workspace_env else None
    builtin_env = os.getenv("OPENHERON_BUILTIN_SKILLS_DIR")
    builtin_skills_dir = Path(builtin_env).expanduser() if builtin_env else None
    return SkillRegistry(workspace=workspace, builtin_skills_dir=builtin_skills_dir)


def list_skills() -> str:
    """ADK tool: list available skills as JSON."""
    registry = get_registry()
    payload = [
        {
            "name": info.name,
            "description": info.description,
            "source": info.source,
            "location": str(info.path),
        }
        for info in registry.list_skills()
    ]
    output = json.dumps(payload, ensure_ascii=False, indent=2)
    if _debug_enabled():
        logger.debug("[DEBUG] {}: {}", "tool.list_skills.output", _debug_body(output))
    return output


def read_skill(name: str) -> str:
    """ADK tool: read a specific SKILL.md by name."""
    content = get_registry().read_skill(name)
    if _debug_enabled():
        logger.debug(
            "[DEBUG] {}: {}",
            "tool.read_skill.output",
            _debug_body({"name": name, "chars": len(content)}),
        )
    return content


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _debug_enabled() -> bool:
    return env_enabled("OPENHERON_DEBUG", default=False)


def _debug_body(payload: Any) -> str:
    """Serialize debug payloads consistently while keeping callsite in this file."""
    try:
        return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return str(payload)
