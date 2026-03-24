#!/usr/bin/env python3
"""Minimal smoke runner for openpipixia GUI tools.

Usage:
  python scripts/gui_smoke.py --mode single --action "click browser icon"
  python scripts/gui_smoke.py --mode task --task "open browser and search openpipixia"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from openpipixia.core.config import bootstrap_env_from_config, get_data_dir, get_config_path
from openpipixia.tooling.registry import computer_task, computer_use


def _global_config_path() -> Path:
    return get_data_dir() / "global_config.json"


def _enabled_agent_names() -> list[str]:
    path = _global_config_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []

    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, list):
        return []

    names: list[str] = []
    seen: set[str] = set()
    for item in agents_raw:
        name = ""
        enabled = True
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("id") or "").strip()
            enabled = bool(item.get("enabled", True))
        if not name or not enabled or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _resolve_bootstrap_config_path(explicit: str) -> Path | None:
    if explicit.strip():
        return Path(explicit).expanduser()

    default_path = get_config_path()
    if default_path.exists():
        return default_path

    for agent_name in _enabled_agent_names():
        agent_path = get_data_dir() / agent_name / "config.json"
        if agent_path.exists():
            return agent_path
    return None


def bootstrap_gui_smoke_env(config_path: str = "") -> Path | None:
    resolved = _resolve_bootstrap_config_path(config_path)
    if resolved is None:
        return None
    loaded = bootstrap_env_from_config(resolved)
    if loaded is None:
        return None
    return resolved


def _enable_debug_log(explicit_path: str = "") -> str:
    path = explicit_path.strip()
    if not path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(tempfile.gettempdir(), f"openpipixia-gui-smoke-{stamp}.debug.log")
    os.environ["OPENPIPIXIA_DEBUG"] = "1"
    os.environ["OPENPIPIXIA_DEBUG_LOG_PATH"] = path
    return path


def _print_payload_summary(payload: dict[str, object]) -> None:
    if "steps" in payload and isinstance(payload.get("steps"), list):
        print("\n=== Task Summary ===")
        print(f"ok={payload.get('ok')} status={payload.get('status_code')} steps={payload.get('step_count')}")
        for step in payload["steps"]:
            if not isinstance(step, dict):
                continue
            print(
                f"- step={step.get('step')} type={step.get('type')} ok={step.get('ok')} "
                f"action={step.get('action')} changed={step.get('screen_changed')} retries={step.get('retries_used')}"
            )
            planner_raw = str(step.get("planner_raw_model_output", "") or "").strip()
            executor_raw = str(step.get("executor_raw_model_output", "") or "").strip()
            screenshots = step.get("screenshots")
            if planner_raw:
                print(f"  planner_raw: {planner_raw}")
            if executor_raw:
                print(f"  executor_raw: {executor_raw}")
            if isinstance(screenshots, dict):
                before_path = screenshots.get("before_path")
                after_path = screenshots.get("after_path")
                if before_path or after_path:
                    print(f"  screenshots: before={before_path} after={after_path}")
        return

    print("\n=== Action Summary ===")
    print(
        f"ok={payload.get('ok')} action={payload.get('action')} "
        f"changed={payload.get('screen_changed')} retries={payload.get('retries_used')}"
    )
    raw_model_output = str(payload.get("raw_model_output", "") or "").strip()
    screenshots = payload.get("screenshots")
    if raw_model_output:
        print(f"raw_model_output: {raw_model_output}")
    if isinstance(screenshots, dict):
        before_path = screenshots.get("before_path")
        after_path = screenshots.get("after_path")
        if before_path or after_path:
            print(f"screenshots: before={before_path} after={after_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test for openpipixia GUI automation tools.")
    parser.add_argument("--mode", choices=["single", "task"], default="single")
    parser.add_argument("--action", default="", help="Single-step action text for computer_use.")
    parser.add_argument("--task", default="", help="Multi-step task text for computer_task.")
    parser.add_argument("--max-steps", type=int, default=8, help="Max steps for task mode.")
    parser.add_argument("--dry-run", action="store_true", help="Run grounding without real GUI actions.")
    parser.add_argument(
        "--config-path",
        default="",
        help="Optional config path to bootstrap env from before running the smoke test.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging to a local file.")
    parser.add_argument(
        "--debug-log-path",
        default="",
        help="Optional debug log file path used with --debug.",
    )
    args = parser.parse_args()
    config_path = bootstrap_gui_smoke_env(args.config_path)
    debug_log_path = ""
    if args.debug:
        debug_log_path = _enable_debug_log(args.debug_log_path)
        print(f"[gui-smoke] debug log: {debug_log_path}")
    if config_path is not None:
        print(f"[gui-smoke] config: {config_path}")

    if args.mode == "single":
        if not args.action.strip():
            print("Error: --action is required when --mode=single")
            return 2
        raw = computer_use(action=args.action, dry_run=args.dry_run)
    else:
        if not args.task.strip():
            print("Error: --task is required when --mode=task")
            return 2
        raw = computer_task(task=args.task, max_steps=args.max_steps, dry_run=args.dry_run)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(raw)
        return 1

    _print_payload_summary(payload)
    if debug_log_path:
        print(f"[gui-smoke] inspect debug log: {debug_log_path}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
