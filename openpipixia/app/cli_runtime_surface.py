"""Runtime-facing CLI command implementations extracted from cli.py.

This module keeps command logic for skills/mcps/spawn so the main cli module
can focus on argument wiring and top-level dispatch.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from ..tooling.skills_adapter import get_registry


async def collect_connected_mcp_apis(
    toolsets: list[Any],
    *,
    timeout_seconds: float,
    get_tools_fn: Callable[[Any], Any],
) -> dict[str, list[dict[str, str]]]:
    """Fetch API details for already-connected MCP toolsets."""

    def _pick_schema(raw_tool: Any, schema_name: str) -> Any:
        value = getattr(raw_tool, schema_name, None)
        if value is not None:
            return value
        if schema_name == "inputSchema":
            return getattr(raw_tool, "input_schema", None)
        if schema_name == "outputSchema":
            return getattr(raw_tool, "output_schema", None)
        return None

    def _schema_summary(schema: Any) -> str:
        if schema is None:
            return "(未声明)"
        if isinstance(schema, dict):
            schema_type = str(schema.get("type", ""))
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            if isinstance(properties, dict) and properties:
                required_names = set(required) if isinstance(required, list) else set()
                names: list[str] = []
                for key in properties.keys():
                    key_str = str(key)
                    if key_str in required_names:
                        names.append(f"{key_str}(required)")
                    else:
                        names.append(key_str)
                prefix = f"type={schema_type}; " if schema_type else ""
                return f"{prefix}fields={', '.join(names)}"
            if schema_type:
                return f"type={schema_type}"
        try:
            rendered = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            rendered = str(schema)
        if len(rendered) > 240:
            rendered = rendered[:237] + "..."
        return rendered

    async def _collect_one(toolset: Any) -> tuple[str, list[dict[str, str]]]:
        api_rows: list[dict[str, str]] = []
        try:
            tools = await asyncio.wait_for(
                get_tools_fn(toolset),
                timeout=max(1.0, float(timeout_seconds)),
            )
        except Exception:
            return toolset.meta.name, api_rows
        for tool in tools:
            name = str(getattr(tool, "name", "") or "").strip()
            if not name:
                continue
            raw_tool = getattr(tool, "raw_mcp_tool", None)
            description = ""
            input_summary = "(未声明)"
            output_summary = "(未声明)"
            if raw_tool is not None:
                description = (
                    str(getattr(raw_tool, "description", "") or getattr(raw_tool, "title", "") or "").strip()
                )
                input_summary = _schema_summary(_pick_schema(raw_tool, "inputSchema"))
                output_summary = _schema_summary(_pick_schema(raw_tool, "outputSchema"))
            if not description:
                description = str(getattr(tool, "description", "") or "").strip() or "(未提供)"
            api_rows.append(
                {
                    "name": name,
                    "description": description,
                    "input": input_summary,
                    "output": output_summary,
                }
            )
        api_rows.sort(key=lambda item: item.get("name", ""))
        return toolset.meta.name, api_rows

    pairs = await asyncio.gather(*[_collect_one(toolset) for toolset in toolsets])
    return {name: rows for name, rows in pairs}


def cmd_skills(
    *,
    agent: str | None,
    stdout_line: Callable[[str], None],
    resolve_target_agent_names: Callable[[str | None], tuple[list[str], str | None]],
    run_agent_cli_command: Callable[[str, list[str]], tuple[int, str, str]],
) -> int:
    """List skills for one agent or aggregated multi-agent view."""
    target_agents, error = resolve_target_agent_names(agent)
    if error:
        stdout_line(error)
        return 1
    if target_agents:
        if agent:
            code, out, err = run_agent_cli_command(target_agents[0], ["skills"])
            if out.strip():
                stdout_line(out.strip())
            if err.strip():
                stdout_line(err.strip())
            return 0 if code == 0 else 1

        merged: list[dict[str, Any]] = []
        failures: list[str] = []
        for agent_name in target_agents:
            code, out, err = run_agent_cli_command(agent_name, ["skills"])
            if code != 0:
                detail = err.strip() or out.strip() or f"exit_code={code}"
                failures.append(f"{agent_name}: {detail}")
                continue
            try:
                payload = json.loads(out)
            except Exception:
                failures.append(f"{agent_name}: invalid JSON output")
                continue
            if not isinstance(payload, list):
                failures.append(f"{agent_name}: invalid JSON payload type")
                continue
            for item in payload:
                if not isinstance(item, dict):
                    continue
                row = dict(item)
                row["agent"] = agent_name
                merged.append(row)
        stdout_line(json.dumps(merged, ensure_ascii=False, indent=2))
        if failures:
            stdout_line(f"[warn] skills failed for agents: {'; '.join(failures)}")
            return 1
        return 0

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
    stdout_line(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_mcps(
    *,
    agent: str | None,
    stdout_line: Callable[[str], None],
    resolve_target_agent_names: Callable[[str | None], tuple[list[str], str | None]],
    run_agent_cli_command: Callable[[str, list[str]], tuple[int, str, str]],
    print_agent_output_sections: Callable[[list[tuple[str, int, str, str]]], int],
    load_mcp_probe_policy: Callable[..., Any],
    build_mcp_toolsets_from_env_fn: Callable[..., list[Any]],
    probe_mcp_toolsets_fn: Callable[..., Any],
    collect_connected_mcp_apis_fn: Callable[..., Any],
) -> int:
    """List connected MCP servers and available APIs for each server."""
    target_agents, error = resolve_target_agent_names(agent)
    if error:
        stdout_line(error)
        return 1
    if target_agents:
        results: list[tuple[str, int, str, str]] = []
        for agent_name in target_agents:
            code, out, err = run_agent_cli_command(agent_name, ["mcps"])
            results.append((agent_name, code, out, err))
        return print_agent_output_sections(results)

    toolsets = build_mcp_toolsets_from_env_fn(log_registered=False)
    if not toolsets:
        stdout_line("MCP: no servers configured")
        return 0

    async def _run_mcps() -> tuple[int, list[dict[str, Any]], dict[str, list[dict[str, str]]]]:
        probe_policy = load_mcp_probe_policy(
            timeout_env_name="OPENPIPIXIA_MCP_LIST_TIMEOUT_SECONDS",
            timeout_default=5.0,
        )
        toolsets_by_name = {toolset.meta.name: toolset for toolset in toolsets}
        try:
            results = await probe_mcp_toolsets_fn(
                toolsets,
                timeout_seconds=probe_policy.timeout_seconds,
                retry_attempts=probe_policy.retry_attempts,
                retry_backoff_seconds=probe_policy.retry_backoff_seconds,
            )
            connected_names = [str(item.get("name", "")) for item in results if str(item.get("status")) == "ok"]
            if not connected_names:
                return 0, results, {}
            connected_toolsets = [toolsets_by_name[name] for name in connected_names if name in toolsets_by_name]
            api_names_by_server = await collect_connected_mcp_apis_fn(
                connected_toolsets,
                timeout_seconds=probe_policy.timeout_seconds,
            )
            return 0, results, api_names_by_server
        finally:
            for toolset in toolsets:
                try:
                    await toolset.close()
                except Exception:
                    continue

    try:
        code, results, api_names_by_server = asyncio.run(_run_mcps())
    except Exception as exc:
        stdout_line(f"MCP probe failed: {exc}")
        return 1
    if code != 0:
        return code

    connected = [item for item in results if str(item.get("status")) == "ok"]
    if not connected:
        stdout_line("MCP: no connected servers")
        return 0

    stdout_line(f"Connected MCP servers: {len(connected)}")
    stdout_line("")
    for item in connected:
        server_name = str(item.get("name", "unknown"))
        transport = str(item.get("transport", "unknown"))
        api_rows = api_names_by_server.get(server_name, [])
        stdout_line(f"- {server_name} ({transport}) | APIs: {len(api_rows)}")
        if not api_rows:
            stdout_line("  (none)")
            stdout_line("")
            continue
        for api in api_rows:
            api_name = str(api.get("name", "")).strip()
            api_description = str(api.get("description", "(未提供)")).strip() or "(未提供)"
            stdout_line(f"  - {api_name}: {api_description}")
        stdout_line("")
    return 0


def cmd_spawn(
    *,
    agent: str | None,
    stdout_line: Callable[[str], None],
    resolve_target_agent_names: Callable[[str | None], tuple[list[str], str | None]],
    run_agent_cli_command: Callable[[str, list[str]], tuple[int, str, str]],
    print_agent_output_sections: Callable[[list[tuple[str, int, str, str]]], int],
    read_subagent_records: Callable[[int], list[dict[str, Any]]],
) -> int:
    """List sub-agent tasks created by `spawn_subagent`."""
    target_agents, error = resolve_target_agent_names(agent)
    if error:
        stdout_line(error)
        return 1
    if target_agents:
        results: list[tuple[str, int, str, str]] = []
        for agent_name in target_agents:
            code, out, err = run_agent_cli_command(agent_name, ["spawn"])
            results.append((agent_name, code, out, err))
        return print_agent_output_sections(results)

    records = read_subagent_records(50)
    if not records:
        stdout_line("Subagents: none")
        return 0

    stdout_line(f"Subagents: {len(records)} recent task(s)")
    for item in records:
        task_id = str(item.get("task_id", "unknown"))
        status = str(item.get("status", "unknown"))
        channel = str(item.get("channel", "unknown"))
        chat_id = str(item.get("chat_id", "unknown"))
        created_at = str(item.get("timestamp", ""))
        prompt_preview = str(item.get("prompt_preview", "")).strip()
        stdout_line(
            f"- {task_id} status={status} target={channel}:{chat_id} created_at={created_at}"
        )
        if prompt_preview:
            stdout_line(f"  prompt: {prompt_preview}")
    return 0
