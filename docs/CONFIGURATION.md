# openpipixia 配置说明

## 配置来源与优先级

支持三种配置来源：

- 基础配置（推荐）：`~/.openpipixia/<agent_name>/config.json`
- 高级运行配置：`~/.openpipixia/<agent_name>/runtime.json`
- 环境变量（在未配置时作为回退）

优先级规则：

- 每个 Agent 的 `config.json` / `runtime.json` 中已配置的字段会覆盖同名环境变量
- 当对应 Agent 的 `config.json` 不存在，或文件内容为空对象 `{}` 时，直接使用环境变量

建议日常只维护当前 Agent 的 `config.json`，将性能/运行时调优项放在对应的 `runtime.json`，环境变量用于无配置回退或临时排查。

## Agent 创建与目录布局

推荐先用 CLI 创建 Agent：

```bash
ppx create --name "assistant-main"
ppx create --name "operator-main" --role operator
```

创建后会得到类似目录结构：

- `~/.openpipixia/assistant-main/config.json`
- `~/.openpipixia/assistant-main/runtime.json`
- `~/.openpipixia/global_config.json`

说明：

- `agent_name` 取自 `ppx create --name`，会自动去掉特殊字符，并把空格替换成 `-`
- 新 Agent 默认会被写入并启用到 `global_config.json`
- 如果运行 gateway，建议显式传入对应 Agent 的 `--config-path`
- 可以用 `ppx list` 查看已有 Agent、角色、workspace 和启用状态

## `config.json` 关键字段

- `agent.name / agent.role / agent.permissions / agent.workspace / agent.builtinSkillsDir`
- `providers.<provider>.enabled / apiKey / model / apiBase / extraHeaders`
- `multimodalProviders.<alias>.enabled / provider / apiKey / model / apiBase / extraHeaders`
- `gui.groundingProvider / gui.plannerProvider / gui.builtinGUIToolsEnabled`（绑定到 `multimodalProviders` 名称）
- `channels.<name>.*`
- `web.enabled` / `web.search.*`
- `security.restrictToWorkspace / allowExec / allowNetwork / execAllowlist`
- `tools.mcpServers`（每个 server 支持 `enabled`，默认 `true`）
- `debug`

Provider 选择由 `enabled` 控制，建议保持“仅一个 provider 为 true”。

### `agent.role` 与默认权限

当前内置三种角色：

- `Assistant`：低权限，默认单 workspace、文件只读、不可执行 shell、不可访问网络
- `Operator`：执行型，默认单 workspace 读写、受限 shell、受限网络
- `Manager`：高权限，允许更宽的执行与网络能力

第一版实现会把角色默认权限同步映射到现有 `security.*` 和运行时环境变量上。

### Channel 配置示例

下面是一段可直接合并到 agent `config.json` 的示例：

```json
{
  "channels": {
    "local": {
      "enabled": true
    },
    "weixin": {
      "enabled": true,
      "baseUrl": "https://ilinkai.weixin.qq.com",
      "token": "",
      "stateDir": "",
      "pollTimeoutSeconds": 35,
      "allowFrom": []
    },
    "wecom": {
      "enabled": false,
      "botId": "",
      "secret": "",
      "allowFrom": [],
      "welcomeMessage": ""
    }
  }
}
```

说明：
- `weixin.token` 一般留空，先执行 `ppx channels login weixin`
- `weixin.stateDir` 留空时会使用默认运行时目录
- `wecom` 需要先安装 `pip install -e .[wecom]`
- `weixin` 若要稳定使用二维码登录和媒体收发，建议安装 `pip install -e .[weixin]`

## `runtime.json`（高级）关键字段

- `env`（可选）：通用环境变量覆盖映射，支持任意运行时 env 配置项

当你需要配置尚未结构化到 `config.json` 字段中的运行时开关时，在 `runtime.json` 中使用 `env`：

```json
{
  "env": {
    "OPENPIPIXIA_MEMORY_ENABLED": "0",
    "OPENPIPIXIA_MCP_REQUIRED_SERVERS": "filesystem,notion",
    "OPENPIPIXIA_DEBUG_MAX_CHARS": 4000
  }
}
```

默认生成的 `runtime.json` 已包含常见运行时开关的默认值（如 memory/compaction/mcp probe/debug chars 等），可直接在 `env` 段内修改。

兼容说明：历史版本中写在 `config.json.env` 的内容仍可读取；后续保存配置时会迁移到 `runtime.json`。

## 常用环境变量

### Provider / Runtime

- `GOOGLE_API_KEY`
- `OPENAI_API_KEY`
- `OPENPIPIXIA_CHANNELS`
- `OPENPIPIXIA_DEBUG`
- `OPENPIPIXIA_DEBUG_MAX_CHARS`

### Session / Memory / Compaction

- `OPENPIPIXIA_SESSION_DB_URL`
- `OPENPIPIXIA_MEMORY_ENABLED`
- `OPENPIPIXIA_MEMORY_BACKEND`
- `OPENPIPIXIA_MEMORY_MARKDOWN_DIR`
- `OPENPIPIXIA_COMPACTION_ENABLED`
- `OPENPIPIXIA_COMPACTION_INTERVAL`
- `OPENPIPIXIA_COMPACTION_OVERLAP`
- `OPENPIPIXIA_COMPACTION_TOKEN_THRESHOLD`
- `OPENPIPIXIA_COMPACTION_EVENT_RETENTION`

### WhatsApp Bridge

- `WHATSAPP_BRIDGE_URL`
- `WHATSAPP_BRIDGE_TOKEN`
- `OPENPIPIXIA_WHATSAPP_BRIDGE_PRECHECK`
- `OPENPIPIXIA_WHATSAPP_BRIDGE_SOURCE`

### Exec / MCP

- `OPENPIPIXIA_EXEC_ALLOWLIST`
- `OPENPIPIXIA_EXEC_SECURITY`
- `OPENPIPIXIA_EXEC_SAFE_BINS`
- `OPENPIPIXIA_EXEC_ASK`
- `OPENPIPIXIA_MCP_SERVERS_JSON`
- `OPENPIPIXIA_MCP_REQUIRED_SERVERS`
- `OPENPIPIXIA_MCP_PROBE_RETRY_ATTEMPTS`
- `OPENPIPIXIA_MCP_PROBE_RETRY_BACKOFF_SECONDS`
- `OPENPIPIXIA_MCP_DOCTOR_TIMEOUT_SECONDS`
- `OPENPIPIXIA_MCP_GATEWAY_TIMEOUT_SECONDS`
- `OPENPIPIXIA_GUI_MCP_NAME`
- `OPENPIPIXIA_GUI_MCP_TRANSPORT`
- `OPENPIPIXIA_GUI_BUILTIN_TOOLS_ENABLED`

### GUI Automation

- GUI 执行链路已固定为 ADK-only：不再使用 `OPENPIPIXIA_GUI_USE_ADK_GROUNDING`、`OPENPIPIXIA_GUI_TASK_USE_ADK_PLANNER` 开关。
- `OPENPIPIXIA_GUI_MODEL`
- `OPENPIPIXIA_GUI_BASE_URL`
- `OPENPIPIXIA_GUI_PLANNER_MODEL`
- `OPENPIPIXIA_GUI_PLANNER_BASE_URL`
- `OPENPIPIXIA_GUI_GROUNDING_PROVIDER`
- `OPENPIPIXIA_GUI_PLANNER_PROVIDER`
- `OPENPIPIXIA_GUI_MAX_PARSE_RETRIES`
- `OPENPIPIXIA_GUI_MAX_ACTION_RETRIES`
- `OPENPIPIXIA_GUI_VERIFY_SCREEN_CHANGE`
- `OPENPIPIXIA_GUI_MAX_WAIT_SECONDS`
- `OPENPIPIXIA_GUI_ALLOW_DANGEROUS_KEYS`
- `OPENPIPIXIA_GUI_ALLOWED_ACTIONS`
- `OPENPIPIXIA_GUI_BLOCKED_ACTIONS`
- `OPENPIPIXIA_GUI_TASK_MAX_STEPS`
- `OPENPIPIXIA_GUI_TASK_PARSE_RETRIES`
- `OPENPIPIXIA_GUI_TASK_MAX_NO_PROGRESS_STEPS`
- `OPENPIPIXIA_GUI_TASK_MAX_REPEAT_ACTIONS`

### GUI 多模态 Provider（config.json）

当你希望 GUI 的 grounding/planner 使用 `config.json` 中的多模态模型配置，并允许两者使用不同模型时，配置：

```json
{
  "multimodalProviders": {
    "grounding_mm": {
      "enabled": true,
      "provider": "openai",
      "apiKey": "your_grounding_key",
      "model": "gpt-4.1-mini",
      "apiBase": "",
      "extraHeaders": {}
    },
    "planner_mm": {
      "enabled": true,
      "provider": "openai",
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
- `multimodalProviders.<alias>.provider` 建议显式填写（如 `openai` / `google`），用于 provider 识别与 API key env 映射
- `gui.groundingProvider` 对应 `OPENPIPIXIA_GUI_MODEL/API_KEY/BASE_URL`
- `gui.plannerProvider` 对应 `OPENPIPIXIA_GUI_PLANNER_MODEL/API_KEY/BASE_URL`
- 若 `provider` 为空，则兼容旧行为：回退使用 `<alias>` 作为 provider 名
- provider 未配置或 `enabled=false` 时，不会从 `config.json` 注入对应 GUI 环境变量

## 不太常见变量速查（含意义）

布尔型变量统一支持：`1/0`、`true/false`、`on/off`、`yes/no`。

### Memory / Session / Context Compaction

| 变量 | 默认值 | 作用 | 何时需要设置 |
|---|---|---|---|
| `OPENPIPIXIA_SESSION_DB_URL` | 自动生成 SQLite 路径 | 覆盖会话数据库地址 | 需要把 session 存到自定义数据库时 |
| `OPENPIPIXIA_MEMORY_ENABLED` | `1` | 是否启用 ADK memory 写入链路 | 临时排查 memory 行为时可设为 `0` |
| `OPENPIPIXIA_MEMORY_BACKEND` | `markdown` | 选择 memory 后端：`markdown`（默认）或 `in_memory`（调试） | 仅在本地调试临时关闭落盘时使用 `in_memory` |
| `OPENPIPIXIA_MEMORY_MARKDOWN_DIR` | `~/.openpipixia/workspace/memory` | Markdown memory 根目录 | 需要把记忆落盘到指定目录时 |
| `OPENPIPIXIA_COMPACTION_ENABLED` | `1` | 是否启用 ADK events compaction | 需要原样保留完整事件流时可关掉 |
| `OPENPIPIXIA_COMPACTION_INTERVAL` | `8` | 每隔多少事件触发一次 compaction 检查（最小为 1） | 长对话频繁撑窗口时可适当调小 |
| `OPENPIPIXIA_COMPACTION_OVERLAP` | `1` | 相邻压缩片段保留的重叠事件数 | 希望压缩后上下文衔接更稳时可调大 |
| `OPENPIPIXIA_COMPACTION_TOKEN_THRESHOLD` | 未设置 | token 阈值触发条件 | 需要按 token 体积控制压缩节奏时 |
| `OPENPIPIXIA_COMPACTION_EVENT_RETENTION` | 未设置 | token 压缩时至少保留的近期事件数 | 与 `TOKEN_THRESHOLD` 配对使用 |

注意：`OPENPIPIXIA_COMPACTION_TOKEN_THRESHOLD` 和 `OPENPIPIXIA_COMPACTION_EVENT_RETENTION` 必须成对设置；只设一个会被忽略。

### MCP（健康检查与强依赖）

| 变量 | 默认值 | 作用 | 何时需要设置 |
|---|---|---|---|
| `OPENPIPIXIA_MCP_SERVERS_JSON` | `{}` | 直接注入 MCP server 配置 JSON | 临时覆盖 `config.json` 中的 MCP 配置 |
| `OPENPIPIXIA_MCP_REQUIRED_SERVERS` | 空 | 声明“必须可用”的 MCP 服务名列表 | 某些 MCP 工具是生产强依赖时 |
| `OPENPIPIXIA_MCP_DOCTOR_TIMEOUT_SECONDS` | `5`（范围 1..30） | `doctor` 命令探测 MCP 超时时间 | MCP 服务响应较慢时 |
| `OPENPIPIXIA_MCP_GATEWAY_TIMEOUT_SECONDS` | `5`（范围 1..30） | gateway 启动前探测 required MCP 超时 | 启动阶段经常误判超时时 |
| `OPENPIPIXIA_MCP_PROBE_RETRY_ATTEMPTS` | `2`（范围 1..5） | MCP 探测失败重试次数 | 网络抖动场景下提高稳定性 |
| `OPENPIPIXIA_MCP_PROBE_RETRY_BACKOFF_SECONDS` | `0.3`（范围 0..5） | MCP 探测重试退避基数（秒） | 控制探测重试节奏 |

### WhatsApp Bridge 与其他运行开关

| 变量 | 默认值 | 作用 | 何时需要设置 |
|---|---|---|---|
| `WHATSAPP_BRIDGE_URL` | 空（配置文件通常为 `ws://localhost:3001`） | WhatsApp bridge WebSocket 地址 | 开启 whatsapp 通道时必须可用 |
| `WHATSAPP_BRIDGE_TOKEN` | 空 | WhatsApp bridge 鉴权 token | bridge 启用 token 鉴权时 |
| `OPENPIPIXIA_WHATSAPP_BRIDGE_PRECHECK` | `1` | gateway/doctor 是否先做 bridge 可达性检查 | 本地调试临时跳过预检查可设 `0` |
| `OPENPIPIXIA_WHATSAPP_BRIDGE_SOURCE` | 空 | 指定 bridge 源码目录（含 `package.json`） | bridge 资源不在默认位置时 |
| `OPENPIPIXIA_SUBAGENT_MAX_CONCURRENCY` | `2`（范围 1..16） | 并发子代理任务上限 | 子任务吞吐或资源占用需要调优时 |
| `OPENPIPIXIA_DEBUG_MAX_CHARS` | `2000`（范围 200..20000） | debug 日志中单段文本最大长度 | 排查长 prompt 截断时可调大 |

### GUI Automation（动作与任务编排）

| 变量 | 默认值 | 作用 | 何时需要设置 |
|---|---|---|---|
| `OPENPIPIXIA_GUI_MODEL` | 空 | `computer_use` 的 grounding 模型 | 启用 GUI 单步工具时必填 |
| `OPENPIPIXIA_GUI_BASE_URL` | 空 | grounding 模型 API Base URL | 使用兼容网关或私有部署时 |
| `OPENPIPIXIA_GUI_PLANNER_MODEL` | 空（回退 `OPENPIPIXIA_GUI_MODEL`） | `computer_task` 多步 planner 模型 | 启用 GUI 多步工具时建议显式设置 |
| `OPENPIPIXIA_GUI_PLANNER_BASE_URL` | 空（回退 GUI base_url） | planner API Base URL | planner 与 executor 走不同网关时 |
| `OPENPIPIXIA_GUI_GROUNDING_PROVIDER` | 空 | GUI grounding 使用的 provider 名称（如 `google` / `openai`） | 需要按 provider 环境变量自动取 key 时 |
| `OPENPIPIXIA_GUI_PLANNER_PROVIDER` | 空 | GUI planner 使用的 provider 名称（为空时回退 grounding provider） | planner 与 grounding provider 不同 |
| `OPENPIPIXIA_GUI_MAX_PARSE_RETRIES` | `1` | `computer_use` 解析模型输出失败时的重试次数 | 模型输出不稳定时增加 |
| `OPENPIPIXIA_GUI_MAX_ACTION_RETRIES` | `1` | `computer_use` 在无屏幕变化时动作重试次数 | GUI 响应偶发慢时增加 |
| `OPENPIPIXIA_GUI_VERIFY_SCREEN_CHANGE` | `true` | 是否启用前后截图变化校验 | 调试阶段可临时设为 `false` |
| `OPENPIPIXIA_GUI_MAX_WAIT_SECONDS` | `5.0` | `wait` 动作最大等待时长上限 | 任务需要更长等待时调大 |
| `OPENPIPIXIA_GUI_ALLOW_DANGEROUS_KEYS` | `false` | 是否允许危险快捷键组合 | 默认应保持 `false` |
| `OPENPIPIXIA_GUI_ALLOWED_ACTIONS` | 空 | 允许动作白名单（逗号分隔） | 限制执行面时 |
| `OPENPIPIXIA_GUI_BLOCKED_ACTIONS` | 空 | 禁止动作黑名单（逗号分隔） | 禁止特定动作时 |
| `OPENPIPIXIA_GUI_TASK_MAX_STEPS` | `8` | `computer_task` 最大步骤数 | 任务复杂度较高时增加 |
| `OPENPIPIXIA_GUI_TASK_PARSE_RETRIES` | `1` | planner JSON 解析重试次数 | planner 输出不稳定时增加 |
| `OPENPIPIXIA_GUI_TASK_MAX_NO_PROGRESS_STEPS` | `3` | 连续无进展步骤阈值，触发 `status_code=no_progress` | 防止任务空转时 |
| `OPENPIPIXIA_GUI_TASK_MAX_REPEAT_ACTIONS` | `3` | 连续重复同动作阈值，触发 `status_code=no_progress` | 防止重复动作死循环时 |

推荐最小配置（GUI）：

```bash
export OPENPIPIXIA_GUI_MODEL=gpt-4.1-mini
export OPENPIPIXIA_GUI_PLANNER_MODEL=gpt-4.1-mini
export OPENPIPIXIA_GUI_GROUNDING_PROVIDER=openai
export OPENAI_API_KEY=your_api_key
```

可选策略配置示例（限制动作范围）：

```bash
export OPENPIPIXIA_GUI_ALLOWED_ACTIONS=wait,left_click,double_click,type,key,scroll
export OPENPIPIXIA_GUI_BLOCKED_ACTIONS=right_click,left_click_drag
export OPENPIPIXIA_GUI_ALLOW_DANGEROUS_KEYS=false
```

## 配置样例

```json
{
  "agent": {
    "workspace": "~/.openpipixia/workspace",
    "builtinSkillsDir": ""
  },
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
      "openpipixia_gui": {
        "enabled": true,
        "command": "openpipixia-gui-mcp",
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

WhatsApp bridge 依赖 Node.js `>=20`，运行时目录位于 `~/.openpipixia/bridge/`。
