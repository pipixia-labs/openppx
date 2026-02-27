# openheron 配置说明

## 配置来源与优先级

支持三种配置来源：

- 基础配置（推荐）：`~/.openheron/config.json`
- 高级运行配置：`~/.openheron/runtime.json`
- 环境变量（在未配置时作为回退）

优先级规则：

- `config.json` / `runtime.json` 中已配置的字段会覆盖同名环境变量
- 当 `config.json` 不存在，或文件内容为空对象 `{}` 时，直接使用环境变量

建议日常只维护 `config.json`，将性能/运行时调优项放在 `runtime.json`，环境变量用于无配置回退或临时排查。

## `config.json` 关键字段

- `agent.workspace` / `agent.builtinSkillsDir`
- `agents.defaults / agents.list / bindings`（多智能体路由与每 agent 策略）
- `providers.<provider>.enabled / apiKey / model / apiBase / extraHeaders`
- `multimodalProviders.<name>.enabled / apiKey / model / apiBase / extraHeaders`
- `gui.groundingProvider / gui.plannerProvider / gui.builtinToolsEnabled`（绑定到 `multimodalProviders` 名称）
- `session.dbUrl`
- `channels.<name>.*`
- `web.enabled` / `web.search.*`
- `security.restrictToWorkspace / allowExec / allowNetwork / execAllowlist`
- `tools.mcpServers`（每个 server 支持 `enabled`，默认 `true`）
- `debug`

Provider 选择由 `enabled` 控制，建议保持“仅一个 provider 为 true”。

## 多智能体配置（v1）

v1 路由键：`channel + accountId + peer`，优先级为：

1. `peer`（`channel + accountId + peer.kind + peer.id`）
2. `account`（`channel + accountId`）
3. `channel`（`channel`）
4. `default agent`

`agents.defaults` 用于公共默认值，`agents.list` 定义具体 agent，`bindings` 定义路由规则。

每个 agent 可独立配置：

- `workspace`
- `agentDir`（包含该 agent 的 auth 缓存目录）
- `security`（`restrictToWorkspace/allowExec/allowNetwork/execAllowlist`）
- `fs`（`workspaceOnly/allowedPaths/denyPaths/readOnlyPaths`）
- `tools`（`allow/deny`）
- `skills`（skill allowlist）
- `systemPermissions`（如 `browser/gui/screenshot`）

说明：

- DM 会话默认按 peer 隔离（并区分 accountId）。
- 支持同一 channel 下多 accountId 路由到不同 agent。
- `match.guild / match.team / match.roles` 已支持静态校验与 `doctor/routes lint` 摘要展示；
  当前运行时路由仍按 `channel + accountId + peer` 生效（后续版本再接入主体级匹配）。

## `runtime.json`（高级）关键字段

- `env`（可选）：通用环境变量覆盖映射，支持任意运行时 env 配置项

当你需要配置尚未结构化到 `config.json` 字段中的运行时开关时，在 `runtime.json` 中使用 `env`：

```json
{
  "env": {
    "OPENHERON_MEMORY_ENABLED": "0",
    "OPENHERON_MCP_REQUIRED_SERVERS": "filesystem,notion",
    "OPENHERON_DEBUG_MAX_CHARS": 4000
  }
}
```

默认由 `openheron install --init-only` 生成的 `runtime.json` 已包含常见运行时开关的默认值（如 memory/compaction/mcp probe/debug chars 等），可直接在 `env` 段内修改。

兼容说明：历史版本中写在 `config.json.env` 的内容仍可读取；后续保存配置时会迁移到 `runtime.json`。

## 常用环境变量

### Provider / Runtime

- `GOOGLE_API_KEY`
- `OPENAI_API_KEY`
- `OPENHERON_CHANNELS`
- `OPENHERON_DEBUG`
- `OPENHERON_DEBUG_MAX_CHARS`
- `OPENHERON_AGENT_DIR`

### Session / Memory / Compaction

- `OPENHERON_SESSION_DB_URL`
- `OPENHERON_MEMORY_ENABLED`
- `OPENHERON_MEMORY_BACKEND`
- `OPENHERON_MEMORY_MARKDOWN_DIR`
- `OPENHERON_COMPACTION_ENABLED`
- `OPENHERON_COMPACTION_INTERVAL`
- `OPENHERON_COMPACTION_OVERLAP`
- `OPENHERON_COMPACTION_TOKEN_THRESHOLD`
- `OPENHERON_COMPACTION_EVENT_RETENTION`

### WhatsApp Bridge

- `WHATSAPP_BRIDGE_URL`
- `WHATSAPP_BRIDGE_TOKEN`
- `OPENHERON_WHATSAPP_BRIDGE_PRECHECK`
- `OPENHERON_WHATSAPP_BRIDGE_SOURCE`

### Exec / MCP

- `OPENHERON_EXEC_ALLOWLIST`
- `OPENHERON_EXEC_SECURITY`
- `OPENHERON_EXEC_SAFE_BINS`
- `OPENHERON_EXEC_ASK`
- `OPENHERON_MCP_SERVERS_JSON`
- `OPENHERON_MCP_REQUIRED_SERVERS`
- `OPENHERON_MCP_PROBE_RETRY_ATTEMPTS`
- `OPENHERON_MCP_PROBE_RETRY_BACKOFF_SECONDS`
- `OPENHERON_MCP_DOCTOR_TIMEOUT_SECONDS`
- `OPENHERON_MCP_GATEWAY_TIMEOUT_SECONDS`
- `OPENHERON_GUI_MCP_NAME`
- `OPENHERON_GUI_MCP_TRANSPORT`
- `OPENHERON_GUI_BUILTIN_TOOLS_ENABLED`

### GUI Automation

- GUI 执行链路已固定为 ADK-only：不再使用 `OPENHERON_GUI_USE_ADK_GROUNDING`、`OPENHERON_GUI_TASK_USE_ADK_PLANNER` 开关。
- `OPENHERON_GUI_MODEL`
- `OPENHERON_GUI_API_KEY`
- `OPENHERON_GUI_BASE_URL`
- `OPENHERON_GUI_PLANNER_MODEL`
- `OPENHERON_GUI_PLANNER_API_KEY`
- `OPENHERON_GUI_PLANNER_BASE_URL`
- `OPENHERON_GUI_MAX_PARSE_RETRIES`
- `OPENHERON_GUI_MAX_ACTION_RETRIES`
- `OPENHERON_GUI_VERIFY_SCREEN_CHANGE`
- `OPENHERON_GUI_MAX_WAIT_SECONDS`
- `OPENHERON_GUI_ALLOW_DANGEROUS_KEYS`
- `OPENHERON_GUI_ALLOWED_ACTIONS`
- `OPENHERON_GUI_BLOCKED_ACTIONS`
- `OPENHERON_GUI_TASK_MAX_STEPS`
- `OPENHERON_GUI_TASK_PARSE_RETRIES`
- `OPENHERON_GUI_TASK_MAX_NO_PROGRESS_STEPS`
- `OPENHERON_GUI_TASK_MAX_REPEAT_ACTIONS`

### GUI 多模态 Provider（config.json）

当你希望 GUI 的 grounding/planner 使用 `config.json` 中的多模态模型配置，并允许两者使用不同模型时，配置：

```json
{
  "multimodalProviders": {
    "grounding_mm": {
      "enabled": true,
      "apiKey": "your_grounding_key",
      "model": "gpt-4.1-mini",
      "apiBase": "",
      "extraHeaders": {}
    },
    "planner_mm": {
      "enabled": true,
      "apiKey": "your_planner_key",
      "model": "gpt-4.1",
      "apiBase": "",
      "extraHeaders": {}
    }
  },
  "gui": {
    "groundingProvider": "grounding_mm",
    "plannerProvider": "planner_mm"
  }
}
```

说明：
- `gui.groundingProvider` 对应 `OPENHERON_GUI_MODEL/API_KEY/BASE_URL`
- `gui.plannerProvider` 对应 `OPENHERON_GUI_PLANNER_MODEL/API_KEY/BASE_URL`
- provider 未配置或 `enabled=false` 时，不会从 `config.json` 注入对应 GUI 环境变量

## 不太常见变量速查（含意义）

布尔型变量统一支持：`1/0`、`true/false`、`on/off`、`yes/no`。

### Memory / Session / Context Compaction

| 变量 | 默认值 | 作用 | 何时需要设置 |
|---|---|---|---|
| `OPENHERON_SESSION_DB_URL` | 自动生成 SQLite 路径 | 覆盖会话数据库地址 | 需要把 session 存到自定义数据库时 |
| `OPENHERON_MEMORY_ENABLED` | `1` | 是否启用 ADK memory 写入链路 | 临时排查 memory 行为时可设为 `0` |
| `OPENHERON_MEMORY_BACKEND` | `markdown` | 选择 memory 后端：`markdown`（默认）或 `in_memory`（调试） | 仅在本地调试临时关闭落盘时使用 `in_memory` |
| `OPENHERON_MEMORY_MARKDOWN_DIR` | `~/.openheron/workspace/memory` | Markdown memory 根目录 | 需要把记忆落盘到指定目录时 |
| `OPENHERON_COMPACTION_ENABLED` | `1` | 是否启用 ADK events compaction | 需要原样保留完整事件流时可关掉 |
| `OPENHERON_COMPACTION_INTERVAL` | `8` | 每隔多少事件触发一次 compaction 检查（最小为 1） | 长对话频繁撑窗口时可适当调小 |
| `OPENHERON_COMPACTION_OVERLAP` | `1` | 相邻压缩片段保留的重叠事件数 | 希望压缩后上下文衔接更稳时可调大 |
| `OPENHERON_COMPACTION_TOKEN_THRESHOLD` | 未设置 | token 阈值触发条件 | 需要按 token 体积控制压缩节奏时 |
| `OPENHERON_COMPACTION_EVENT_RETENTION` | 未设置 | token 压缩时至少保留的近期事件数 | 与 `TOKEN_THRESHOLD` 配对使用 |

注意：`OPENHERON_COMPACTION_TOKEN_THRESHOLD` 和 `OPENHERON_COMPACTION_EVENT_RETENTION` 必须成对设置；只设一个会被忽略。

### MCP（健康检查与强依赖）

| 变量 | 默认值 | 作用 | 何时需要设置 |
|---|---|---|---|
| `OPENHERON_MCP_SERVERS_JSON` | `{}` | 直接注入 MCP server 配置 JSON | 临时覆盖 `config.json` 中的 MCP 配置 |
| `OPENHERON_MCP_REQUIRED_SERVERS` | 空 | 声明“必须可用”的 MCP 服务名列表 | 某些 MCP 工具是生产强依赖时 |
| `OPENHERON_MCP_DOCTOR_TIMEOUT_SECONDS` | `5`（范围 1..30） | `doctor` 命令探测 MCP 超时时间 | MCP 服务响应较慢时 |
| `OPENHERON_MCP_GATEWAY_TIMEOUT_SECONDS` | `5`（范围 1..30） | gateway 启动前探测 required MCP 超时 | 启动阶段经常误判超时时 |
| `OPENHERON_MCP_PROBE_RETRY_ATTEMPTS` | `2`（范围 1..5） | MCP 探测失败重试次数 | 网络抖动场景下提高稳定性 |
| `OPENHERON_MCP_PROBE_RETRY_BACKOFF_SECONDS` | `0.3`（范围 0..5） | MCP 探测重试退避基数（秒） | 控制探测重试节奏 |

### WhatsApp Bridge 与其他运行开关

| 变量 | 默认值 | 作用 | 何时需要设置 |
|---|---|---|---|
| `WHATSAPP_BRIDGE_URL` | 空（配置文件通常为 `ws://localhost:3001`） | WhatsApp bridge WebSocket 地址 | 开启 whatsapp 通道时必须可用 |
| `WHATSAPP_BRIDGE_TOKEN` | 空 | WhatsApp bridge 鉴权 token | bridge 启用 token 鉴权时 |
| `OPENHERON_WHATSAPP_BRIDGE_PRECHECK` | `1` | gateway/doctor 是否先做 bridge 可达性检查 | 本地调试临时跳过预检查可设 `0` |
| `OPENHERON_WHATSAPP_BRIDGE_SOURCE` | 空 | 指定 bridge 源码目录（含 `package.json`） | bridge 资源不在默认位置时 |
| `OPENHERON_SUBAGENT_MAX_CONCURRENCY` | `2`（范围 1..16） | 并发子代理任务上限 | 子任务吞吐或资源占用需要调优时 |
| `OPENHERON_DEBUG_MAX_CHARS` | `2000`（范围 200..20000） | debug 日志中单段文本最大长度 | 排查长 prompt 截断时可调大 |

### GUI Automation（动作与任务编排）

| 变量 | 默认值 | 作用 | 何时需要设置 |
|---|---|---|---|
| `OPENHERON_GUI_MODEL` | 空 | `computer_use` 的 grounding 模型 | 启用 GUI 单步工具时必填 |
| `OPENHERON_GUI_API_KEY` | 空（回退 `OPENAI_API_KEY`） | grounding 模型 API Key | 与 `OPENHERON_GUI_MODEL` 配套 |
| `OPENHERON_GUI_BASE_URL` | 空 | grounding 模型 API Base URL | 使用兼容网关或私有部署时 |
| `OPENHERON_GUI_PLANNER_MODEL` | 空（回退 `OPENHERON_GUI_MODEL`） | `computer_task` 多步 planner 模型 | 启用 GUI 多步工具时建议显式设置 |
| `OPENHERON_GUI_PLANNER_API_KEY` | 空（回退 GUI key / `OPENAI_API_KEY`） | planner API Key | planner 与 executor 分离配置时 |
| `OPENHERON_GUI_PLANNER_BASE_URL` | 空（回退 GUI base_url） | planner API Base URL | planner 与 executor 走不同网关时 |
| `OPENHERON_GUI_MAX_PARSE_RETRIES` | `1` | `computer_use` 解析模型输出失败时的重试次数 | 模型输出不稳定时增加 |
| `OPENHERON_GUI_MAX_ACTION_RETRIES` | `1` | `computer_use` 在无屏幕变化时动作重试次数 | GUI 响应偶发慢时增加 |
| `OPENHERON_GUI_VERIFY_SCREEN_CHANGE` | `true` | 是否启用前后截图变化校验 | 调试阶段可临时设为 `false` |
| `OPENHERON_GUI_MAX_WAIT_SECONDS` | `5.0` | `wait` 动作最大等待时长上限 | 任务需要更长等待时调大 |
| `OPENHERON_GUI_ALLOW_DANGEROUS_KEYS` | `false` | 是否允许危险快捷键组合 | 默认应保持 `false` |
| `OPENHERON_GUI_ALLOWED_ACTIONS` | 空 | 允许动作白名单（逗号分隔） | 限制执行面时 |
| `OPENHERON_GUI_BLOCKED_ACTIONS` | 空 | 禁止动作黑名单（逗号分隔） | 禁止特定动作时 |
| `OPENHERON_GUI_TASK_MAX_STEPS` | `8` | `computer_task` 最大步骤数 | 任务复杂度较高时增加 |
| `OPENHERON_GUI_TASK_PARSE_RETRIES` | `1` | planner JSON 解析重试次数 | planner 输出不稳定时增加 |
| `OPENHERON_GUI_TASK_MAX_NO_PROGRESS_STEPS` | `3` | 连续无进展步骤阈值，触发 `status_code=no_progress` | 防止任务空转时 |
| `OPENHERON_GUI_TASK_MAX_REPEAT_ACTIONS` | `3` | 连续重复同动作阈值，触发 `status_code=no_progress` | 防止重复动作死循环时 |

推荐最小配置（GUI）：

```bash
export OPENHERON_GUI_MODEL=gpt-4.1-mini
export OPENHERON_GUI_PLANNER_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=your_api_key
```

可选策略配置示例（限制动作范围）：

```bash
export OPENHERON_GUI_ALLOWED_ACTIONS=wait,left_click,double_click,type,key,scroll
export OPENHERON_GUI_BLOCKED_ACTIONS=right_click,left_click_drag
export OPENHERON_GUI_ALLOW_DANGEROUS_KEYS=false
```

## 配置样例

```json
{
  "agent": {
    "workspace": "~/.openheron/workspace",
    "builtinSkillsDir": ""
  },
  "agents": {
    "defaults": {
      "workspace": "~/.openheron/workspace",
      "agentDir": "~/.openheron/agents/main/agent",
      "security": {
        "restrictToWorkspace": false,
        "allowExec": true,
        "allowNetwork": true,
        "execAllowlist": []
      },
      "fs": {
        "workspaceOnly": false,
        "allowedPaths": [],
        "denyPaths": [],
        "readOnlyPaths": []
      },
      "tools": {
        "allow": [],
        "deny": []
      },
      "systemPermissions": {
        "browser": true,
        "gui": true,
        "screenshot": true
      }
    },
    "list": [
      {
        "id": "main",
        "default": true,
        "workspace": "~/.openheron/workspace/main",
        "agentDir": "~/.openheron/agents/main/agent",
        "skills": []
      },
      {
        "id": "biz",
        "workspace": "~/.openheron/workspace/biz",
        "agentDir": "~/.openheron/agents/biz/agent",
        "skills": [
          "sales"
        ],
        "tools": {
          "deny": [
            "exec"
          ]
        },
        "systemPermissions": {
          "gui": false
        }
      }
    ]
  },
  "bindings": [
    {
      "agentId": "biz",
      "match": {
        "channel": "whatsapp",
        "accountId": "business"
      }
    },
    {
      "agentId": "main",
      "match": {
        "channel": "whatsapp",
        "accountId": "personal"
      }
    }
  ],
  "providers": {
    "google": {
      "enabled": true,
      "apiKey": "your_google_api_key",
      "model": "gemini-3-flash-preview"
    },
    "openai": {
      "enabled": false,
      "apiKey": "",
      "model": "openai/gpt-4.1-mini"
    }
  },
  "session": {
    "dbUrl": ""
  },
  "channels": {
    "local": {
      "enabled": false
    },
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "encryptKey": "",
      "verificationToken": ""
    }
  },
  "web": {
    "enabled": true,
    "search": {
      "enabled": true,
      "provider": "brave",
      "apiKey": "your_brave_api_key",
      "maxResults": 5
    }
  },
  "security": {
    "restrictToWorkspace": false,
    "allowExec": true,
    "allowNetwork": true,
    "execAllowlist": []
  },
  "tools": {
    "mcpServers": {
      "filesystem": {
        "enabled": true,
        "command": "npx",
        "args": [
          "-y",
          "@modelcontextprotocol/server-filesystem",
          "/absolute/path/to/workspace"
        ]
      },
      "openheron_gui": {
        "enabled": true,
        "command": "openheron-gui-mcp",
        "args": [],
        "toolNamePrefix": "mcp_gui_",
        "requireConfirmation": true
      }
    }
  },
  "debug": false
}
```

## 平台说明

### Feishu

如果环境使用 SOCKS 代理，Feishu websocket 依赖 `python-socks`（默认依赖已包含）。

### WhatsApp

WhatsApp bridge 依赖 Node.js `>=20`，运行时目录位于 `~/.openheron/bridge/`。
