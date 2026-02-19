# MCP 与安全策略

## MCP Tool Integration（最小接入）

`sentientagent_v2` 使用 ADK `McpToolset`，从 `tools.mcpServers` 读取服务配置。

### 每个服务可配置字段

- `command` + `args` + `env`：stdio MCP 服务
- `url`：远端 MCP 服务
- `transport`：可选，`sse` / `http`
- `headers`：远端请求头
- `toolFilter`（或 `tool_filter`）：暴露工具白名单
- `toolNamePrefix`（或 `tool_name_prefix`）：工具名前缀
- `requireConfirmation`（或 `require_confirmation`）：调用确认

### 最小验证流程

1. 在 `~/.sentientagent_v2/config.json` 配置 `tools.mcpServers`
2. 执行 `sentientagent_v2 doctor` 查看服务健康状态与工具列表
3. 启动 `sentientagent_v2 gateway`
4. 在对话中调用 MCP 工具（例如 `mcp_filesystem_...`）

### 常用 MCP 环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `SENTIENTAGENT_V2_MCP_DOCTOR_TIMEOUT_SECONDS` | `5`（范围 1..30） | `doctor` 对 MCP 健康检查超时 |
| `SENTIENTAGENT_V2_MCP_GATEWAY_TIMEOUT_SECONDS` | `5`（范围 1..30） | gateway 启动阶段 required MCP 检查超时 |
| `SENTIENTAGENT_V2_MCP_PROBE_RETRY_ATTEMPTS` | `2`（范围 1..5） | MCP 探测失败重试次数 |
| `SENTIENTAGENT_V2_MCP_PROBE_RETRY_BACKOFF_SECONDS` | `0.3`（范围 0..5） | MCP 探测重试退避基数（秒） |
| `SENTIENTAGENT_V2_MCP_REQUIRED_SERVERS` | 空 | 指定必须健康的 MCP 服务列表（逗号分隔） |

如果设置了 `SENTIENTAGENT_V2_MCP_REQUIRED_SERVERS`，且某 required server 不可用，gateway 启动会失败（快速失败）。

## 安全策略

`sentientagent_v2` 用统一策略约束文件、命令、网络能力。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `restrictToWorkspace` | `false` | 限制文件工具和 shell 路径参数在 `SENTIENTAGENT_V2_WORKSPACE` 下 |
| `allowExec` | `true` | 全局启用/禁用 `exec` 工具 |
| `allowNetwork` | `true` | 全局启用/禁用 `web_search`/`web_fetch` |
| `execAllowlist` | `[]` | 命令名白名单（空表示不额外限制） |

补充：

- `execAllowlist` 只校验命令名（argv 第一个 token）
- `exec` 默认 `shell=False`，减少 shell 注入面
