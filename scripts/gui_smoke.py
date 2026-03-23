#!/usr/bin/env python3
"""Minimal smoke runner for openpipixia GUI tools.

Usage:
  python scripts/gui_smoke.py --mode single --action "click browser icon"
  python scripts/gui_smoke.py --mode task --task "open browser and search openpipixia"
"""

from __future__ import annotations

import argparse
import json
import sys

from openpipixia.tooling.registry import computer_task, computer_use


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test for openpipixia GUI automation tools.")
    parser.add_argument("--mode", choices=["single", "task"], default="single")
    parser.add_argument("--action", default="", help="Single-step action text for computer_use.")
    parser.add_argument("--task", default="", help="Multi-step task text for computer_task.")
    parser.add_argument("--max-steps", type=int, default=8, help="Max steps for task mode.")
    parser.add_argument("--dry-run", action="store_true", help="Run grounding without real GUI actions.")
    args = parser.parse_args()

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

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
