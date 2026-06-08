# MCP 与安全策略

## MCP Tool Integration（最小接入）

`openppx` 使用 ADK `McpToolset`，从 `tools.mcpServers` 读取服务配置。

### 每个服务可配置字段

- `enabled`：可选，默认 `true`；设为 `false` 时跳过该 MCP 服务
- `command` + `args` + `env`：stdio MCP 服务
- `url`：远端 MCP 服务
- `transport`：可选，`sse` / `http`
- `headers`：远端请求头
- `toolFilter`（或 `tool_filter`）：暴露工具白名单
- `toolNamePrefix`（或 `tool_name_prefix`）：工具名前缀 stem；ADK 会在 prefix 与工具名之间自动加 `_`
- `requireConfirmation`（或 `require_confirmation`）：调用确认
- `runtimeHeaders`（或 `runtime_headers`）：把 ADK 运行时上下文按需映射为远端 MCP 请求头
- `progressEvents`（或 `progress_events`）：是否把 MCP progress notification 转为 openppx step event，默认 `false`
- `longTaskProxy`（或 `long_task_proxy`）：是否让 MCP 工具进入 openppx 长任务 proxy，默认 `true`
- `inlineBudgetMs`（或 `inline_budget_ms`）：MCP 工具调用的内联等待预算，默认 `5000`

`runtimeHeaders` 默认关闭，避免把 user/session 等上下文静默发送给远端服务。支持的 source 包括：

- `user_id`、`session_id`、`app_name`、`invocation_id`、`agent_name`
- `metadata.<key>` / `custom_metadata.<key>` / `run_metadata.<key>`
- `state.<key>`、`session.<attr>`、`literal:<value>`

示例：

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

### MCP 长任务 proxy

`longTaskProxy` 默认开启后，openppx 会包装 ADK 返回的 MCP tools，而不是替换 ADK `McpToolset`。

当前语义：

- MCP 调用在 `inlineBudgetMs` 内完成时，工具结果按原样 inline 返回。
- 超过预算仍未完成时，工具返回 `task_id`，后台 coroutine 在当前进程内继续执行。
- 后台完成或失败会更新 `TaskRun(kind=mcp)` 和 task events。
- 当前进程内仍 attached 的 MCP proxy task 可以 best-effort `interrupt_task` / `cancel_task`。
- 如果进程重启或后台 coroutine 不再 attached，任务会进入 `stale`，后续可收敛为 `lost`。

这里没有承诺通用 MCP server-side cancel/status/checkpoint。只有当具体 MCP server 暴露明确 job 协议时，才适合继续接入 server-specific adapter。

### MCP 外部 job protocol

如果某个 MCP server 的工具会快速返回外部 job id，可显式配置 `jobProtocol`：

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

当前语义：

- 只有配置了 `jobProtocol`，且原 MCP 工具结果能按 `jobIdPath` 取到 job id 时，openppx 才创建 external `TaskRun`。
- `statusTool` 是必须项；没有它就不能声明可 rejoin 的外部 job。
- `outputTool` / `cancelTool` 是可选项；未配置时不会伪造 output/cancel 能力。
- `show_task` / scheduler 会通过 `statusTool` 更新任务状态；状态不可见时任务会进入 `stale`，而不是继续展示成普通 running。
- `cancel_task` 只在 `cancelTool` 存在时展示并调用 provider cancel。

这里仍不承诺 checkpoint/resume，也不自动发现 MCP server 的 job 协议；协议必须由配置显式声明。

### 最小验证流程

1. 在目标 Agent 的 `~/.openppx/<agent_name>/config.json` 配置 `tools.mcpServers`
2. 执行 `ppx doctor` 查看服务健康状态与工具列表
3. 启动 `ppx gateway run`
4. 在对话中调用 MCP 工具（例如 `mcp_filesystem_...`）

### 内置 GUI MCP（推荐）

可将 GUI 能力作为独立 MCP 服务接入，便于统一权限控制：

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

- 工具名：`mcp_gui_gui_action`、`mcp_gui_gui_task`
- `requireConfirmation=true` 可将高风险 GUI 执行纳入确认流
- 细粒度动作控制仍由 GUI 环境变量生效（如 `OPENPPX_GUI_ALLOWED_ACTIONS`）
- 建议同时设置 `OPENPPX_GUI_BUILTIN_TOOLS_ENABLED=0`，让 agent 仅通过 MCP GUI 工具执行

### 常用 MCP 环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `OPENPPX_MCP_DOCTOR_TIMEOUT_SECONDS` | `5`（范围 1..30） | `doctor` 对 MCP 健康检查超时 |
| `OPENPPX_MCP_GATEWAY_TIMEOUT_SECONDS` | `5`（范围 1..30） | gateway 启动阶段 required MCP 检查超时 |
| `OPENPPX_MCP_PROBE_RETRY_ATTEMPTS` | `2`（范围 1..5） | MCP 探测失败重试次数 |
| `OPENPPX_MCP_PROBE_RETRY_BACKOFF_SECONDS` | `0.3`（范围 0..5） | MCP 探测重试退避基数（秒） |
| `OPENPPX_MCP_REQUIRED_SERVERS` | 空 | 指定必须健康的 MCP 服务列表（逗号分隔） |

如果设置了 `OPENPPX_MCP_REQUIRED_SERVERS`，且某 required server 不可用，gateway 启动会失败（快速失败）。

## 安全策略

`openppx` 用统一策略约束文件、命令、网络能力。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `restrictToWorkspace` | `false` | 限制文件工具和 shell 路径参数在 `OPENPPX_WORKSPACE` 下 |
| `allowExec` | `true` | 全局启用/禁用 `exec` 工具 |
| `allowNetwork` | `true` | 全局启用/禁用 `web_search`/`web_fetch` |
| `execAllowlist` | `[]` | 命令名白名单（空表示不额外限制） |

补充：

- `execAllowlist` 在链式命令下会逐段校验命令名（`&&` / `||` / `;`）
- `exec` 默认 `shell=False`，减少 shell 注入面

### Exec 运行时策略（新增）

`exec` 现在支持常见 shell 复合命令（如 `export ... && ...`），并可通过环境变量控制执行策略：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENPPX_EXEC_SECURITY` | 自动（有 allowlist 时=`allowlist`，否则=`full`） | 执行策略：`deny` / `allowlist` / `full` |
| `OPENPPX_EXEC_SAFE_BINS` | 空 | 在 `allowlist` 模式下允许的额外命令名（逗号分隔） |
| `OPENPPX_EXEC_ASK` | `off` | 审批策略：`off` / `on-miss` / `always` |
| `OPENPPX_HIGH_RISK_ACTION_ACCESS` | `true` | 高风险工具策略：`true` 允许，`conditional` 走 ADK 确认，其他值禁用 |

在 root agent / gateway 路径中，`OPENPPX_EXEC_ASK` 和
`OPENPPX_HIGH_RISK_ACTION_ACCESS=conditional` 会使用 ADK 原生
`adk_request_confirmation` 暂停工具调用。用户回复 `yes` / `confirm` /
`approve` 后继续执行，回复 `no` / `reject` / `cancel` 后拒绝执行。直接
调用 Python 工具函数且没有 ADK `tool_context` 时仍会返回
`approval required`，用于保持低层安全边界。
