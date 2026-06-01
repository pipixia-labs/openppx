"""Offline checks for the official ADK eval entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

from google.adk.cli.cli_eval import get_root_agent
from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.eval_set import EvalSet


EVAL_DIR = Path(__file__).parent / "eval"
AGENT_DIR = EVAL_DIR / "openppx"
EVALSET_PATH = EVAL_DIR / "evalsets" / "openppx_smoke.evalset.json"
CONFIG_PATH = EVAL_DIR / "eval_config.json"


def test_eval_entrypoint_directory_basename_matches_production_app_name() -> None:
    assert AGENT_DIR.name == "openppx"


def test_adk_eval_entrypoint_exposes_root_agent() -> None:
    sys.modules.pop("agent", None)
    try:
        root_agent = get_root_agent(str(AGENT_DIR))
    finally:
        sys.modules.pop("agent", None)

    assert root_agent.name == "openppx"
    assert root_agent.tools


def test_evalset_schema_uses_openppx_app_name() -> None:
    eval_set = EvalSet.model_validate_json(EVALSET_PATH.read_text(encoding="utf-8"))

    assert eval_set.eval_set_id == "openppx_smoke"
    assert eval_set.eval_cases[0].session_input is not None
    assert eval_set.eval_cases[0].session_input.app_name == "openppx"


def test_eval_config_schema_loads_default_criteria() -> None:
    config = EvalConfig.model_validate_json(CONFIG_PATH.read_text(encoding="utf-8"))

    assert config.criteria["tool_trajectory_avg_score"] == 1.0
    assert config.criteria["response_match_score"] == 0.8
