"""Offline checks for the official ADK eval entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

from google.adk.cli.cli_eval import get_root_agent
from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.eval_set import EvalSet


EVAL_DIR = Path(__file__).parent / "eval"
AGENT_DIR = EVAL_DIR / "openppx"
CONFIG_PATH = EVAL_DIR / "eval_config.json"
TOOLS_CONFIG_PATH = EVAL_DIR / "eval_config_tools.json"
EVALSET_PATHS = sorted((EVAL_DIR / "evalsets").glob("*.evalset.json"))


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
    assert EVALSET_PATHS

    for path in EVALSET_PATHS:
        eval_set = EvalSet.model_validate_json(path.read_text(encoding="utf-8"))
        assert eval_set.eval_cases
        for case in eval_set.eval_cases:
            assert case.session_input is not None
            assert case.session_input.app_name == "openppx"
            assert case.session_input.user_id
            assert case.conversation
            for invocation in case.conversation:
                assert invocation.invocation_id
                assert invocation.final_response is not None


def test_eval_config_schema_loads_default_criteria() -> None:
    config = EvalConfig.model_validate_json(CONFIG_PATH.read_text(encoding="utf-8"))

    assert config.criteria["tool_trajectory_avg_score"] == 1.0
    assert config.criteria["response_match_score"] == 0.8


def test_tool_eval_config_uses_in_order_trajectory_matching() -> None:
    config = EvalConfig.model_validate_json(TOOLS_CONFIG_PATH.read_text(encoding="utf-8"))

    criterion = config.criteria["tool_trajectory_avg_score"]
    assert getattr(criterion, "threshold") == 1.0
    assert getattr(criterion, "match_type") == "IN_ORDER"


def test_tools_evalset_has_safe_expected_tool_trajectory() -> None:
    path = EVAL_DIR / "evalsets" / "openppx_tools.evalset.json"
    eval_set = EvalSet.model_validate_json(path.read_text(encoding="utf-8"))

    invocation = eval_set.eval_cases[0].conversation[0]
    tool_uses = invocation.intermediate_data.tool_uses
    assert [tool.name for tool in tool_uses] == ["list_skills"]
    assert tool_uses[0].args == {}


def test_memory_evalset_covers_multi_turn_session_recall() -> None:
    path = EVAL_DIR / "evalsets" / "openppx_memory.evalset.json"
    eval_set = EvalSet.model_validate_json(path.read_text(encoding="utf-8"))

    case = eval_set.eval_cases[0]
    assert len(case.conversation) == 2
    assert all(invocation.intermediate_data.tool_uses == [] for invocation in case.conversation)
    assert case.conversation[1].final_response.parts[0].text == "OPENPPX_MEMORY_SESSION_TOKEN"


def test_mcp_evalset_uses_safe_mock_tool() -> None:
    path = EVAL_DIR / "evalsets" / "openppx_mcp.evalset.json"
    eval_set = EvalSet.model_validate_json(path.read_text(encoding="utf-8"))

    invocation = eval_set.eval_cases[0].conversation[0]
    tool_uses = invocation.intermediate_data.tool_uses
    assert [tool.name for tool in tool_uses] == ["mcp_eval_echo_context"]
    assert tool_uses[0].args == {"token": "OPENPPX_MCP_ECHO_OK"}
    assert (EVAL_DIR / "mock_mcp_server.py").exists()


def test_permissions_evalset_expects_no_dangerous_tool_call() -> None:
    path = EVAL_DIR / "evalsets" / "openppx_permissions.evalset.json"
    eval_set = EvalSet.model_validate_json(path.read_text(encoding="utf-8"))

    invocation = eval_set.eval_cases[0].conversation[0]
    assert invocation.intermediate_data.tool_uses == []
    text = invocation.user_content.parts[0].text
    assert "exec" in text
    assert "Do not call any tool" in text


def test_subagent_evalset_covers_no_unnecessary_delegation_boundary() -> None:
    path = EVAL_DIR / "evalsets" / "openppx_subagent.evalset.json"
    eval_set = EvalSet.model_validate_json(path.read_text(encoding="utf-8"))

    invocation = eval_set.eval_cases[0].conversation[0]
    assert invocation.intermediate_data.tool_uses == []
    assert "spawn_subagent" in invocation.user_content.parts[0].text
    assert invocation.final_response.parts[0].text == "OPENPPX_SUBAGENT_BOUNDARY_OK"
