# MCP 与安全策略

## MCP Tool Integration（最小接入）

`openheron` 使用 ADK `McpToolset`，从 `tools.mcpServers` 读取服务配置。

### 每个服务可配置字段

- `command` + `args` + `env`：stdio MCP 服务
- `url`：远端 MCP 服务
- `transport`：可选，`sse` / `http`
- `headers`：远端请求头
- `toolFilter`（或 `tool_filter`）：暴露工具白名单
- `toolNamePrefix`（或 `tool_name_prefix`）：工具名前缀
- `requireConfirmation`（或 `require_confirmation`）：调用确认

### 最小验证流程

1. 在 `~/.openheron/config.json` 配置 `tools.mcpServers`
2. 执行 `openheron doctor` 查看服务健康状态与工具列表
3. 启动 `openheron gateway`
4. 在对话中调用 MCP 工具（例如 `mcp_filesystem_...`）

### 常用 MCP 环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `OPENHERON_MCP_DOCTOR_TIMEOUT_SECONDS` | `5`（范围 1..30） | `doctor` 对 MCP 健康检查超时 |
| `OPENHERON_MCP_GATEWAY_TIMEOUT_SECONDS` | `5`（范围 1..30） | gateway 启动阶段 required MCP 检查超时 |
| `OPENHERON_MCP_PROBE_RETRY_ATTEMPTS` | `2`（范围 1..5） | MCP 探测失败重试次数 |
| `OPENHERON_MCP_PROBE_RETRY_BACKOFF_SECONDS` | `0.3`（范围 0..5） | MCP 探测重试退避基数（秒） |
| `OPENHERON_MCP_REQUIRED_SERVERS` | 空 | 指定必须健康的 MCP 服务列表（逗号分隔） |

如果设置了 `OPENHERON_MCP_REQUIRED_SERVERS`，且某 required server 不可用，gateway 启动会失败（快速失败）。

## 安全策略

`openheron` 用统一策略约束文件、命令、网络能力。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `restrictToWorkspace` | `false` | 限制文件工具和 shell 路径参数在 `OPENHERON_WORKSPACE` 下 |
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
| `OPENHERON_EXEC_SECURITY` | 自动（有 allowlist 时=`allowlist`，否则=`full`） | 执行策略：`deny` / `allowlist` / `full` |
| `OPENHERON_EXEC_SAFE_BINS` | 空 | 在 `allowlist` 模式下允许的额外命令名（逗号分隔） |
| `OPENHERON_EXEC_ASK` | `off` | 审批策略：`off` / `on-miss` / `always` |

当前版本中，`OPENHERON_EXEC_ASK` 触发时会返回 `approval required` 占位错误（尚未接入完整审批流）。
