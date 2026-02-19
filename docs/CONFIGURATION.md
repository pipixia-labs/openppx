# sentientagent_v2 配置说明

## 配置来源与优先级

支持两种配置来源：

- 配置文件（推荐）：`~/.sentientagent_v2/config.json`
- 环境变量（覆盖配置文件）

建议日常只维护 `config.json`，环境变量仅用于临时覆盖。

## `config.json` 关键字段

- `agent.workspace` / `agent.builtinSkillsDir`
- `providers.<provider>.enabled / apiKey / model / apiBase / extraHeaders`
- `session.dbUrl`
- `channels.<name>.*`
- `web.enabled` / `web.search.*`
- `security.restrictToWorkspace / allowExec / allowNetwork / execAllowlist`
- `tools.mcpServers`
- `debug`

Provider 选择由 `enabled` 控制，建议保持“仅一个 provider 为 true”。

## 常用环境变量

### Provider / Runtime

- `GOOGLE_API_KEY`
- `OPENAI_API_KEY`
- `SENTIENTAGENT_V2_CHANNELS`
- `SENTIENTAGENT_V2_DEBUG`
- `SENTIENTAGENT_V2_DEBUG_MAX_CHARS`

### Session / Memory / Compaction

- `SENTIENTAGENT_V2_SESSION_DB_URL`
- `SENTIENTAGENT_V2_MEMORY_ENABLED`
- `SENTIENTAGENT_V2_MEMORY_BACKEND`
- `SENTIENTAGENT_V2_MEMORY_MARKDOWN_DIR`
- `SENTIENTAGENT_V2_COMPACTION_ENABLED`
- `SENTIENTAGENT_V2_COMPACTION_INTERVAL`
- `SENTIENTAGENT_V2_COMPACTION_OVERLAP`
- `SENTIENTAGENT_V2_COMPACTION_TOKEN_THRESHOLD`
- `SENTIENTAGENT_V2_COMPACTION_EVENT_RETENTION`

### WhatsApp Bridge

- `WHATSAPP_BRIDGE_URL`
- `WHATSAPP_BRIDGE_TOKEN`
- `SENTIENTAGENT_V2_WHATSAPP_BRIDGE_PRECHECK`
- `SENTIENTAGENT_V2_WHATSAPP_BRIDGE_SOURCE`

### Exec / MCP

- `SENTIENTAGENT_V2_EXEC_ALLOWLIST`
- `SENTIENTAGENT_V2_MCP_SERVERS_JSON`
- `SENTIENTAGENT_V2_MCP_REQUIRED_SERVERS`
- `SENTIENTAGENT_V2_MCP_PROBE_RETRY_ATTEMPTS`
- `SENTIENTAGENT_V2_MCP_PROBE_RETRY_BACKOFF_SECONDS`
- `SENTIENTAGENT_V2_MCP_DOCTOR_TIMEOUT_SECONDS`
- `SENTIENTAGENT_V2_MCP_GATEWAY_TIMEOUT_SECONDS`

## 不太常见变量速查（含意义）

布尔型变量统一支持：`1/0`、`true/false`、`on/off`、`yes/no`。

### Memory / Session / Context Compaction

| 变量 | 默认值 | 作用 | 何时需要设置 |
|---|---|---|---|
| `SENTIENTAGENT_V2_SESSION_DB_URL` | 自动生成 SQLite 路径 | 覆盖会话数据库地址 | 需要把 session 存到自定义数据库时 |
| `SENTIENTAGENT_V2_MEMORY_ENABLED` | `1` | 是否启用 ADK memory 写入链路 | 临时排查 memory 行为时可设为 `0` |
| `SENTIENTAGENT_V2_MEMORY_BACKEND` | `in_memory` | 选择 memory 后端：`in_memory` 或 `markdown` | 需要本地可审计记忆时改为 `markdown` |
| `SENTIENTAGENT_V2_MEMORY_MARKDOWN_DIR` | `~/.sentientagent_v2/memory` | Markdown memory 根目录 | 需要把记忆落盘到指定目录时 |
| `SENTIENTAGENT_V2_COMPACTION_ENABLED` | `1` | 是否启用 ADK events compaction | 需要原样保留完整事件流时可关掉 |
| `SENTIENTAGENT_V2_COMPACTION_INTERVAL` | `8` | 每隔多少事件触发一次 compaction 检查（最小为 1） | 长对话频繁撑窗口时可适当调小 |
| `SENTIENTAGENT_V2_COMPACTION_OVERLAP` | `1` | 相邻压缩片段保留的重叠事件数 | 希望压缩后上下文衔接更稳时可调大 |
| `SENTIENTAGENT_V2_COMPACTION_TOKEN_THRESHOLD` | 未设置 | token 阈值触发条件 | 需要按 token 体积控制压缩节奏时 |
| `SENTIENTAGENT_V2_COMPACTION_EVENT_RETENTION` | 未设置 | token 压缩时至少保留的近期事件数 | 与 `TOKEN_THRESHOLD` 配对使用 |

注意：`SENTIENTAGENT_V2_COMPACTION_TOKEN_THRESHOLD` 和 `SENTIENTAGENT_V2_COMPACTION_EVENT_RETENTION` 必须成对设置；只设一个会被忽略。

### MCP（健康检查与强依赖）

| 变量 | 默认值 | 作用 | 何时需要设置 |
|---|---|---|---|
| `SENTIENTAGENT_V2_MCP_SERVERS_JSON` | `{}` | 直接注入 MCP server 配置 JSON | 临时覆盖 `config.json` 中的 MCP 配置 |
| `SENTIENTAGENT_V2_MCP_REQUIRED_SERVERS` | 空 | 声明“必须可用”的 MCP 服务名列表 | 某些 MCP 工具是生产强依赖时 |
| `SENTIENTAGENT_V2_MCP_DOCTOR_TIMEOUT_SECONDS` | `5`（范围 1..30） | `doctor` 命令探测 MCP 超时时间 | MCP 服务响应较慢时 |
| `SENTIENTAGENT_V2_MCP_GATEWAY_TIMEOUT_SECONDS` | `5`（范围 1..30） | gateway 启动前探测 required MCP 超时 | 启动阶段经常误判超时时 |
| `SENTIENTAGENT_V2_MCP_PROBE_RETRY_ATTEMPTS` | `2`（范围 1..5） | MCP 探测失败重试次数 | 网络抖动场景下提高稳定性 |
| `SENTIENTAGENT_V2_MCP_PROBE_RETRY_BACKOFF_SECONDS` | `0.3`（范围 0..5） | MCP 探测重试退避基数（秒） | 控制探测重试节奏 |

### WhatsApp Bridge 与其他运行开关

| 变量 | 默认值 | 作用 | 何时需要设置 |
|---|---|---|---|
| `WHATSAPP_BRIDGE_URL` | 空（配置文件通常为 `ws://localhost:3001`） | WhatsApp bridge WebSocket 地址 | 开启 whatsapp 通道时必须可用 |
| `WHATSAPP_BRIDGE_TOKEN` | 空 | WhatsApp bridge 鉴权 token | bridge 启用 token 鉴权时 |
| `SENTIENTAGENT_V2_WHATSAPP_BRIDGE_PRECHECK` | `1` | gateway/doctor 是否先做 bridge 可达性检查 | 本地调试临时跳过预检查可设 `0` |
| `SENTIENTAGENT_V2_WHATSAPP_BRIDGE_SOURCE` | 空 | 指定 bridge 源码目录（含 `package.json`） | bridge 资源不在默认位置时 |
| `SENTIENTAGENT_V2_SUBAGENT_MAX_CONCURRENCY` | `2`（范围 1..16） | 并发子代理任务上限 | 子任务吞吐或资源占用需要调优时 |
| `SENTIENTAGENT_V2_DEBUG_MAX_CHARS` | `2000`（范围 200..20000） | debug 日志中单段文本最大长度 | 排查长 prompt 截断时可调大 |

## 配置样例

```json
{
  "agent": {
    "workspace": "~/.sentientagent_v2/workspace",
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
        "command": "npx",
        "args": [
          "-y",
          "@modelcontextprotocol/server-filesystem",
          "/absolute/path/to/workspace"
        ]
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

WhatsApp bridge 依赖 Node.js `>=20`，运行时目录位于 `~/.sentientagent_v2/bridge/`。
