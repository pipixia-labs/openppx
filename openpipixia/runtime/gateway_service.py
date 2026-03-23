"""Gateway service manifest helpers for launchd/systemd installation flows."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Literal

ServiceManager = Literal["launchd", "systemd", "unsupported"]


def detect_service_manager(platform_name: str | None = None) -> ServiceManager:
    """Return the supported service manager for the current platform name."""

    import sys

    current = (platform_name or sys.platform).strip().lower()
    if current.startswith("darwin"):
        return "launchd"
    if current.startswith("linux"):
        return "systemd"
    return "unsupported"


def gateway_service_name(app_name: str = "openpipixia") -> str:
    """Return a normalized service name used by installation/runtime commands."""

    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", app_name.strip()).strip("-.")
    if not normalized:
        normalized = "openpipixia"
    return f"{normalized}-gateway"


def render_launchd_plist(
    *,
    label: str,
    program: str,
    args: list[str],
    working_directory: str | Path,
    env: dict[str, str] | None = None,
    stdout_path: str | Path | None = None,
    stderr_path: str | Path | None = None,
    keep_alive: bool = True,
) -> str:
    """Render launchd plist XML content for gateway service installation."""

    escaped_label = html.escape(str(label))
    escaped_workdir = html.escape(str(working_directory))
    all_args = [program, *args]
    arg_lines = "\n".join(f"      <string>{html.escape(str(item))}</string>" for item in all_args)

    env_block = ""
    if env:
        env_lines = "\n".join(
            f"      <key>{html.escape(str(key))}</key><string>{html.escape(str(value))}</string>"
            for key, value in sorted(env.items())
        )
        env_block = f"\n    <key>EnvironmentVariables</key>\n    <dict>\n{env_lines}\n    </dict>"

    stdout_block = ""
    if stdout_path:
        stdout_block = f"\n    <key>StandardOutPath</key>\n    <string>{html.escape(str(stdout_path))}</string>"
    stderr_block = ""
    if stderr_path:
        stderr_block = f"\n    <key>StandardErrorPath</key>\n    <string>{html.escape(str(stderr_path))}</string>"
    keep_alive_value = "true" if keep_alive else "false"

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{escaped_label}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{arg_lines}\n"
        "    </array>\n"
        "    <key>WorkingDirectory</key>\n"
        f"    <string>{escaped_workdir}</string>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>KeepAlive</key>\n"
        f"    <{keep_alive_value}/>"
        f"{env_block}"
        f"{stdout_block}"
        f"{stderr_block}\n"
        "</dict>\n"
        "</plist>\n"
    )


def render_systemd_unit(
    *,
    description: str,
    exec_start: str,
    working_directory: str | Path,
    env: dict[str, str] | None = None,
    restart: str = "always",
    after_targets: tuple[str, ...] = ("network-online.target",),
) -> str:
    """Render systemd user service unit content for gateway installation."""

    after = " ".join(target.strip() for target in after_targets if target.strip()) or "default.target"
    env_lines = ""
    if env:
        rendered = []
        for key, value in sorted(env.items()):
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            rendered.append(f'Environment="{key}={escaped}"')
        env_lines = "\n".join(rendered) + "\n"
    return (
        "[Unit]\n"
        f"Description={description}\n"
        f"After={after}\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={working_directory}\n"
        f"ExecStart={exec_start}\n"
        f"Restart={restart}\n"
        f"{env_lines}"
        "\n[Install]\n"
        "WantedBy=default.target\n"
    )
