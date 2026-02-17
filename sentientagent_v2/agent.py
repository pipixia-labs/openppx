"""Google ADK root agent for sentientagent_v2."""

from __future__ import annotations

import os
import platform
from datetime import datetime

from google.adk.agents import LlmAgent

from .skills import get_registry, list_skills, read_skill
from .tools import (
    cron,
    edit_file,
    exec_command,
    list_dir,
    message,
    message_image,
    read_file,
    web_fetch,
    web_search,
    write_file,
)


def _build_instruction() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    runtime = f"{platform.system()} {platform.machine()} / Python"
    workspace = os.getenv("SENTIENTAGENT_V2_WORKSPACE", os.getcwd())
    skills_summary = get_registry().build_summary()

    return f"""You are sentientagent_v2, a lightweight skills-first coding assistant.

Current time: {now}
Runtime: {runtime}
Workspace: {workspace}

Your job:
1. Solve user tasks directly.
2. Use local skills when relevant.
3. Keep responses concise and actionable.

Rules:
- Channel delivery (e.g. local/Feishu) is handled by the gateway runtime.
- Skill loading is file-based (workspace + built-in SKILL.md).
- Before using a skill deeply, call `list_skills` then `read_skill(name)` for the specific skill.
- Do not invent skill content. Always read SKILL.md first.
- Use `message_image(path=..., caption=...)` when a local image file should be delivered to the current channel.
- Prefer these built-in tools for actions: `read_file`, `write_file`, `edit_file`, `list_dir`, `exec`, `web_search`, `web_fetch`, `message`, `message_image`, `cron`.

Available skills:
{skills_summary}
"""


root_agent = LlmAgent(
    name="sentientagent_v2",
    model=os.getenv("SENTIENTAGENT_V2_MODEL", "gemini-3-flash-preview"),
    instruction=_build_instruction(),
    tools=[
        list_skills,
        read_skill,
        read_file,
        write_file,
        edit_file,
        list_dir,
        exec_command,
        web_search,
        web_fetch,
        message,
        message_image,
        cron,
    ],
)
