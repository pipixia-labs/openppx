# MCP and Security Policy

## MCP Tool Integration

`openppx` uses ADK `McpToolset` and reads server definitions from `tools.mcpServers`.

### Per-Server Fields

- `enabled`: optional, defaults to `true`; set to `false` to skip that MCP server
- `command` + `args` + `env`: stdio MCP server configuration
- `url`: remote MCP server URL
- `transport`: optional, `sse` or `http`
- `headers`: remote request headers
- `toolFilter` or `tool_filter`: tool allowlist exposed to the agent
- `toolNamePrefix` or `tool_name_prefix`: tool-name prefix stem; ADK inserts `_` between the prefix and tool name
- `requireConfirmation` or `require_confirmation`: per-tool confirmation policy
- `runtimeHeaders` or `runtime_headers`: maps ADK runtime context into remote MCP request headers
- `progressEvents` or `progress_events`: converts MCP progress notifications into openppx step events; defaults to `false`
- `longTaskProxy` or `long_task_proxy`: routes MCP tools through the openppx long-task proxy; defaults to `true`
- `inlineBudgetMs` or `inline_budget_ms`: inline wait budget for MCP calls; defaults to `5000`

`runtimeHeaders` is disabled by default to avoid silently sending user/session context to remote services. Supported sources include:

- `user_id`, `session_id`, `app_name`, `invocation_id`, `agent_name`
- `metadata.<key>` / `custom_metadata.<key>` / `run_metadata.<key>`
- `state.<key>`, `session.<attr>`, `literal:<value>`

Example:

```json
{
  "tools": {
    "mcpServers": {
      "tenant_api": {
        "url": "https://mcp.example.com/mcp",
        "runtimeHeaders": {
          "X-OpenPPX-User": "user_id",
          "X-OpenPPX-Session": "session_id",
          "X-OpenPPX-Request-Kind": "metadata.request_kind"
        },
        "progressEvents": true,
        "longTaskProxy": true,
        "inlineBudgetMs": 5000
      }
    }
  }
}
```

### MCP Long-Task Proxy

When `longTaskProxy` is enabled, openppx wraps MCP tools returned by ADK. It does not replace ADK `McpToolset`.

Current behavior:

- If the MCP call completes within `inlineBudgetMs`, the tool result is returned inline unchanged.
- If it exceeds the budget, the tool returns a `task_id`, and a background coroutine continues in the current process.
- Completion or failure updates `TaskRun(kind=mcp)` and task events.
- MCP proxy tasks still attached to the current process can be interrupted or canceled on a best-effort basis.
- If the process restarts or the background coroutine is no longer attached, the task enters `stale` and may later be converged to `lost`.

This does not promise generic server-side MCP cancel, status, or checkpoint semantics. Server-specific adapters should be added only when a concrete MCP server exposes an explicit job protocol.

### MCP External Job Protocol

If an MCP server tool quickly returns an external job id, configure `jobProtocol` explicitly:

```json
{
  "jobProtocol": {
    "jobIdPath": "job_id",
    "statusTool": "job_status",
    "statusArgs": {"job_id": "{job_id}"},
    "outputTool": "job_output",
    "cancelTool": "job_cancel"
  }
}
```

Current behavior:

- openppx creates an external `TaskRun` only when `jobProtocol` is configured and the original MCP result contains a job id at `jobIdPath`.
- `statusTool` is required; without it, the job cannot be declared rejoinable.
- `outputTool` and `cancelTool` are optional; missing tools do not create fake output or cancel capability.
- `show_task` and the scheduler use `statusTool` to update task state. When status is invisible, the task becomes `stale` instead of being shown as a normal running task.
- `cancel_task` is offered only when `cancelTool` exists.

This still does not promise checkpoint or resume behavior, and openppx does not auto-discover MCP job protocols. The protocol must be declared in configuration.

### Minimal Validation Flow

1. Configure `tools.mcpServers` in the target agent config at `~/.openppx/<agent_name>/config.json`.
2. Run `ppx doctor` to inspect service health and the tool list.
3. Start `ppx gateway run`.
4. Call MCP tools in conversation, for example `mcp_filesystem_...`.

### Built-In GUI MCP

Expose GUI automation as an MCP service when you want centralized permission control:

```json
{
  "tools": {
    "mcpServers": {
      "openppx_gui": {
        "enabled": true,
        "command": "openppx-gui-mcp",
        "args": [],
        "toolNamePrefix": "mcp_gui",
        "requireConfirmation": true
      }
    }
  }
}
```

- Tool names: `mcp_gui_gui_action`, `mcp_gui_gui_task`
- `requireConfirmation=true` routes high-risk GUI execution through the confirmation flow.
- Fine-grained action controls still come from GUI environment variables such as `OPENPPX_GUI_ALLOWED_ACTIONS`.
- Set `OPENPPX_GUI_BUILTIN_TOOLS_ENABLED=0` if you want the agent to use GUI tools only through MCP.

### Common MCP Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `OPENPPX_MCP_DOCTOR_TIMEOUT_SECONDS` | `5`, range `1..30` | MCP health-check timeout for `doctor` |
| `OPENPPX_MCP_GATEWAY_TIMEOUT_SECONDS` | `5`, range `1..30` | Required MCP check timeout during gateway startup |
| `OPENPPX_MCP_PROBE_RETRY_ATTEMPTS` | `2`, range `1..5` | MCP probe retry count |
| `OPENPPX_MCP_PROBE_RETRY_BACKOFF_SECONDS` | `0.3`, range `0..5` | Base backoff for MCP probe retries |
| `OPENPPX_MCP_REQUIRED_SERVERS` | empty | Comma-separated list of MCP servers that must be healthy |

If `OPENPPX_MCP_REQUIRED_SERVERS` is set and any required server is unavailable, gateway startup fails fast.

## Security Policy

`openppx` uses one policy layer for file, command, and network capabilities.

| Field | Default | Description |
|---|---|---|
| `restrictToWorkspace` | `false` | Restrict file tools and shell path arguments to `OPENPPX_WORKSPACE` |
| `allowExec` | `true` | Enable or disable the `exec` tool globally |
| `allowNetwork` | `true` | Enable or disable `web_search` / `web_fetch` globally |
| `execAllowlist` | `[]` | Command-name allowlist; empty means no extra command-name restriction |

Additional notes:

- `execAllowlist` validates each command segment in chained commands such as `&&`, `||`, and `;`.
- `exec` defaults to `shell=False` to reduce shell injection risk.

### Exec Runtime Policy

`exec` supports common shell compound commands such as `export ... && ...` and can be controlled through environment variables:

| Variable | Default | Description |
|---|---|---|
| `OPENPPX_EXEC_SECURITY` | automatic: `allowlist` when an allowlist exists, otherwise `full` | Execution policy: `deny`, `allowlist`, or `full` |
| `OPENPPX_EXEC_SAFE_BINS` | empty | Extra command names allowed in `allowlist` mode |
| `OPENPPX_EXEC_ASK` | `off` | Approval policy: `off`, `on-miss`, or `always` |
| `OPENPPX_HIGH_RISK_ACTION_ACCESS` | `true` | High-risk tool policy: `true` allows, `conditional` asks for ADK confirmation, other values deny |

In the root-agent and gateway paths, `OPENPPX_EXEC_ASK` and `OPENPPX_HIGH_RISK_ACTION_ACCESS=conditional` use ADK-native `adk_request_confirmation` to pause tool calls. The call continues after `yes`, `confirm`, or `approve`, and is rejected after `no`, `reject`, or `cancel`. Direct Python tool calls without ADK `tool_context` still return `approval required`, preserving the low-level safety boundary.
