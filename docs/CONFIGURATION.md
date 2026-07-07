# openppx Configuration Guide

## Configuration Sources and Precedence

openppx supports three configuration sources:

- Base agent configuration: `~/.openppx/<agent_name>/config.json`
- Advanced runtime configuration: `~/.openppx/<agent_name>/runtime.json`
- Environment variables, used as fallback when no config value exists

Precedence rules:

- Fields configured in an agent's `config.json` or `runtime.json` override same-name environment variables.
- If the target agent `config.json` does not exist, or if it is an empty object `{}`, openppx falls back to environment variables.

For normal use, keep agent identity, provider, channel, and security settings in `config.json`. Put performance and runtime tuning in the matching `runtime.json`. Use shell environment variables for fallback or temporary diagnostics.

## Agent Creation and Directory Layout

Create agents through the CLI:

```bash
ppx create --name "low-main"
ppx create --name "medium-main" --privilege-level medium
ppx create --name "high-main" --privilege-level high
ppx create --name "root-main" --privilege-level root
```

Typical files:

- `~/.openppx/low-main/config.json`
- `~/.openppx/low-main/runtime.json`
- `~/.openppx/global_config.json`

Notes:

- `agent_name` comes from `ppx create --name`; special characters are removed and spaces become `-`.
- New agents are added to and enabled in `global_config.json`.
- For gateway runs, prefer passing the target agent config explicitly with `--config-path`.
- Use `ppx list` to inspect existing agents, privilege levels, workspace paths, and enabled status.
- `~/.openppx/<agent_name>/` is the agent config home and contains `config.json`, `runtime.json`, `skills/`, `memory/`, `AGENTS.md`, and related metadata.
- `agent.workspace` is reserved for code, temporary files, task outputs, and other working artifacts.

## Key `config.json` Fields

- `agent.name / agent.privilegeLevel / agent.permissions / agent.workspace / agent.builtinSkillsDir`
- `providers.<provider>.enabled / apiKey / model / apiBase / extraHeaders / strictToolCalls`
- `multimodalProviders.<alias>.enabled / provider / apiKey / model / apiBase / extraHeaders`
- `gui.groundingProvider / gui.plannerProvider / gui.builtinGUIToolsEnabled`, bound to names under `multimodalProviders`
- `channels.<name>.*`
- `web.enabled / web.search.*`
- `security.restrictToWorkspace / allowExec / allowNetwork / execAllowlist`
- `tools.mcpServers`, where each server supports `enabled`, default `true`
- `debug`

Provider selection is controlled by `enabled`. The recommended setup is to keep only one provider enabled at a time.

DeepSeek's default model is `deepseek-v4-pro`. When `providers.deepseek.strictToolCalls=true`, openppx tries DeepSeek strict Tool Calls and switches the provider base URL to `https://api.deepseek.com/beta`. When disabled, it uses `https://api.deepseek.com`.

### `agent.privilegeLevel` and Default Permissions

Built-in privilege levels:

- `low`: low privilege; single workspace, read-only files, no shell execution, no network access by default
- `medium`: execution-oriented; single workspace read/write, restricted shell, restricted network
- `high`: currently aligned with `root`, reserved for later tightening
- `root`: highest privilege, broad execution and network capability

The implementation maps the privilege-level defaults into existing `security.*` fields and runtime environment variables.

Breaking changes:

- `--role` was removed. Use `--privilege-level`.
- `agent.role` was removed. Use `agent.privilegeLevel`.
- Legacy values such as `assistant` and `operator` are no longer accepted.

### Channel Configuration Example

This snippet can be merged into an agent `config.json`:

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

Notes:

- Leave `weixin.token` empty in normal use and run `ppx channels login weixin`.
- If `weixin.stateDir` is empty, the default runtime state directory is used.
- Install WeCom support with `pip install -e .[wecom]`.
- For reliable Weixin QR login and media receive/send, install `pip install -e .[weixin]`.

## `runtime.json` Advanced Fields

- `env`, optional: generic environment override mapping that supports any runtime env setting

Use `runtime.json` for runtime switches that are not structured into `config.json`:

```json
{
  "env": {
    "OPENPPX_MEMORY_ENABLED": "0",
    "OPENPPX_MCP_REQUIRED_SERVERS": "filesystem,notion",
    "OPENPPX_DEBUG_MAX_CHARS": 4000
  }
}
```

The default generated `runtime.json` already contains common runtime defaults such as memory, compaction, MCP probe settings, and debug character limits. Edit the `env` block directly.

Compatibility note: legacy `config.json.env` is still read, but later config saves migrate this content to `runtime.json`.

### Forcing Skill API Sandbox from Configuration

For production or high-risk agents, force declarative Command/Python/Node skill APIs into Docker through trusted runtime configuration:

```json
{
  "env": {
    "OPENPPX_SKILL_API_SANDBOX": "docker",
    "OPENPPX_SANDBOX_IMAGE": "openppx-sandbox:dev"
  }
}
```

This is intentionally configured in `runtime.json`, not in skill recipes. Skill files are project content and can be modified by model-generated code or third-party packages. Runtime configuration is the trusted policy boundary.

When `OPENPPX_SKILL_API_SANDBOX=docker` is set:

- Command, Python, and Node declarative skill APIs enter Docker by default.
- Recipe values such as `sandbox: false` or `sandbox: "none"` cannot disable the policy.
- A recipe request for a weaker backend such as `bwrap` is rejected as a backend downgrade.
- Recipe network or image options are honored only when allowed by trusted runtime gates such as `OPENPPX_SANDBOX_ALLOW_NETWORK` or `OPENPPX_SANDBOX_TRUSTED_IMAGES`.

For regular `exec_command` default sandboxing, use:

```json
{
  "env": {
    "OPENPPX_EXEC_SANDBOX": "docker"
  }
}
```

## Skill API Recipes

`invoke_skill_api(skill_name, api_name, args=...)` runs dynamic skill APIs inside the supervised execution envelope. Skill authors do not need to declare whether an API is short or long. The call waits up to `inline_budget_ms`; if it does not finish in time, it is exposed as a `TaskRun` and returns a `task_id`.

Supported API forms:

- Script: `scripts/{api}.py`, `scripts/{api}.sh`, or another executable script
- HTTP recipe: `apis/{api}.json`, `apis/{api}.http.json`
- Python SDK recipe: `apis/{api}.python.json`, `apis/{api}.sdk.json`
- Node recipe: `apis/{api}.node.json`
- Command recipe: `apis/{api}.command.json`

Python SDK recipes support declarative module/function calls only. They do not execute arbitrary string code. Example:

```json
{
  "module": "demo_sdk",
  "function": "search",
  "kwargs": {
    "query": "{query}",
    "limit": "{limit}"
  }
}
```

The `callable` form is also supported:

```json
{
  "callable": "demo_sdk:Client.run",
  "args": ["{args}"]
}
```

The Python runner only allows recipes to reference local Python modules under the skill root. If a third-party SDK is needed, create a thin wrapper module inside the skill and import the SDK from there. `{name}` templates read values from `args`; when the full string is exactly one template, the original JSON type is preserved.

## Common Environment Variables

### Provider / Runtime

- `GOOGLE_API_KEY`
- `OPENAI_API_KEY`
- `OPENPPX_CHANNELS`
- `OPENPPX_DEBUG`
- `OPENPPX_DEBUG_MAX_CHARS`

### Session / Memory / Compaction

- `OPENPPX_SESSION_DB_URL`
- `OPENPPX_MEMORY_ENABLED`
- `OPENPPX_MEMORY_BACKEND`
- `OPENPPX_MEMORY_MARKDOWN_DIR`
- `OPENPPX_COMPACTION_ENABLED`
- `OPENPPX_COMPACTION_INTERVAL`
- `OPENPPX_COMPACTION_OVERLAP`
- `OPENPPX_COMPACTION_TOKEN_THRESHOLD`
- `OPENPPX_COMPACTION_EVENT_RETENTION`

### WhatsApp Bridge

- `WHATSAPP_BRIDGE_URL`
- `WHATSAPP_BRIDGE_TOKEN`
- `OPENPPX_WHATSAPP_BRIDGE_PRECHECK`
- `OPENPPX_WHATSAPP_BRIDGE_SOURCE`

### Exec / MCP

- `OPENPPX_EXEC_ALLOWLIST`
- `OPENPPX_EXEC_SECURITY`
- `OPENPPX_EXEC_SAFE_BINS`
- `OPENPPX_EXEC_ASK`
- `OPENPPX_HIGH_RISK_ACTION_ACCESS`

### Docker Sandbox

Docker sandbox does not change execution behavior by default. `exec_command` can explicitly request `sandbox="docker"`. Declarative skill APIs can be forced into Docker with trusted configuration `OPENPPX_SKILL_API_SANDBOX=docker`. Recipe `"sandbox": {"required": true}` is only an optional request and must not be treated as the safety boundary. See [`SANDBOX.md`](./SANDBOX.md).

- `OPENPPX_SANDBOX_BACKEND`
- `OPENPPX_EXEC_SANDBOX`
- `OPENPPX_SKILL_API_SANDBOX`
- `OPENPPX_SANDBOX_DOCKER_BIN`
- `OPENPPX_SANDBOX_IMAGE`
- `OPENPPX_SANDBOX_PYTHON_BASE_IMAGE`
- `OPENPPX_SANDBOX_PYTHON_REQUIREMENTS`
- `OPENPPX_SANDBOX_NODE_PACKAGE_JSON`
- `OPENPPX_SANDBOX_NODE_PACKAGE_LOCK`
- `OPENPPX_SANDBOX_ALLOW_NETWORK`
- `OPENPPX_SANDBOX_NETWORK_LOCK`
- `OPENPPX_SANDBOX_TRUSTED_IMAGES`
- `OPENPPX_SANDBOX_TIMEOUT_MAX_SECONDS`
- `OPENPPX_SANDBOX_MEMORY`
- `OPENPPX_SANDBOX_CPUS`
- `OPENPPX_SANDBOX_PIDS_LIMIT`
- `OPENPPX_SANDBOX_TMPFS_SIZE`

`OPENPPX_SANDBOX_ALLOW_NETWORK` and `OPENPPX_SANDBOX_TRUSTED_IMAGES` are trusted policy gates. They must not be generated by the model or by dynamic recipes.

### MCP and GUI

- `OPENPPX_MCP_SERVERS_JSON`
- `OPENPPX_MCP_REQUIRED_SERVERS`
- `OPENPPX_MCP_PROBE_RETRY_ATTEMPTS`
- `OPENPPX_MCP_PROBE_RETRY_BACKOFF_SECONDS`
- `OPENPPX_MCP_DOCTOR_TIMEOUT_SECONDS`
- `OPENPPX_MCP_GATEWAY_TIMEOUT_SECONDS`
- `OPENPPX_GUI_MCP_NAME`
- `OPENPPX_GUI_MCP_TRANSPORT`
- `OPENPPX_GUI_BUILTIN_TOOLS_ENABLED`
- `OPENPPX_SYNC_PROXY_INLINE_BUDGET_MS`
- `OPENPPX_SYNC_PROXY_MAX_WORKERS`

### GUI Automation

- The GUI execution path is fixed to ADK-only. `OPENPPX_GUI_USE_ADK_GROUNDING` and `OPENPPX_GUI_TASK_USE_ADK_PLANNER` are no longer used.
- `OPENPPX_GUI_MODEL`
- `OPENPPX_GUI_BASE_URL`
- `OPENPPX_GUI_PLANNER_MODEL`
- `OPENPPX_GUI_PLANNER_BASE_URL`
- `OPENPPX_GUI_GROUNDING_PROVIDER`
- `OPENPPX_GUI_PLANNER_PROVIDER`
- `OPENPPX_GUI_MAX_PARSE_RETRIES`
- `OPENPPX_GUI_MAX_ACTION_RETRIES`
- `OPENPPX_GUI_VERIFY_SCREEN_CHANGE`
- `OPENPPX_GUI_MAX_WAIT_SECONDS`
- `OPENPPX_GUI_ALLOW_DANGEROUS_KEYS`
- `OPENPPX_GUI_ALLOWED_ACTIONS`
- `OPENPPX_GUI_BLOCKED_ACTIONS`
- `OPENPPX_GUI_TASK_MAX_STEPS`
- `OPENPPX_GUI_TASK_PARSE_RETRIES`
- `OPENPPX_GUI_TASK_MAX_NO_PROGRESS_STEPS`
- `OPENPPX_GUI_TASK_MAX_REPEAT_ACTIONS`

### GUI Multimodal Providers in `config.json`

Configure this when GUI grounding and planning should use multimodal provider settings from `config.json`, possibly with different models:

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

Notes:

- Set `multimodalProviders.<alias>.provider` explicitly, for example `openai` or `google`, so provider detection and API-key environment mapping are unambiguous.
- `gui.groundingProvider` maps to `OPENPPX_GUI_MODEL`, API key, and base URL.
- `gui.plannerProvider` maps to `OPENPPX_GUI_PLANNER_MODEL`, API key, and base URL.
- If `provider` is empty, legacy behavior uses `<alias>` as the provider name.
- If the provider is missing or `enabled=false`, GUI environment variables are not injected from `config.json`.

## Less Common Environment Variables

Boolean variables accept `1/0`, `true/false`, `on/off`, and `yes/no`.

### Memory / Session / Context Compaction

| Variable | Default | Purpose | When to set it |
|---|---|---|---|
| `OPENPPX_SESSION_DB_URL` | auto-generated SQLite path | Override session database URL | Store sessions in a custom database |
| `OPENPPX_MEMORY_ENABLED` | `1` | Enable ADK memory write path | Temporarily debug memory behavior |
| `OPENPPX_MEMORY_BACKEND` | `markdown` | Choose `markdown` or `in_memory` | Use `in_memory` for local debugging without disk writes |
| `OPENPPX_MEMORY_MARKDOWN_DIR` | `~/.openppx/<agent_name>/memory` | Markdown memory root | Store memory in a custom directory |
| `OPENPPX_COMPACTION_ENABLED` | `1` | Enable ADK events compaction | Preserve full event streams exactly |
| `OPENPPX_COMPACTION_INTERVAL` | `8` | Event interval between compaction checks, minimum `1` | Compact more often in long conversations |
| `OPENPPX_COMPACTION_OVERLAP` | `1` | Number of overlapping events between compacted windows | Preserve more context continuity |
| `OPENPPX_COMPACTION_TOKEN_THRESHOLD` | unset | Token threshold trigger | Control compaction by token volume |
| `OPENPPX_COMPACTION_EVENT_RETENTION` | unset | Recent event count retained during token compaction | Use with `TOKEN_THRESHOLD` |

`OPENPPX_COMPACTION_TOKEN_THRESHOLD` and `OPENPPX_COMPACTION_EVENT_RETENTION` must be set together. If only one is set, both are ignored.

### MCP Health and Required Servers

| Variable | Default | Purpose | When to set it |
|---|---|---|---|
| `OPENPPX_MCP_SERVERS_JSON` | `{}` | Inject MCP server config JSON directly | Temporarily override `config.json` MCP settings |
| `OPENPPX_MCP_REQUIRED_SERVERS` | empty | Declare MCP server names that must be available | Production depends on specific MCP tools |
| `OPENPPX_MCP_DOCTOR_TIMEOUT_SECONDS` | `5`, range `1..30` | MCP probe timeout for `doctor` | MCP services respond slowly |
| `OPENPPX_MCP_GATEWAY_TIMEOUT_SECONDS` | `5`, range `1..30` | Required MCP probe timeout during gateway startup | Startup probes time out too often |
| `OPENPPX_MCP_PROBE_RETRY_ATTEMPTS` | `2`, range `1..5` | MCP probe retry count | Smooth out transient failures |
| `OPENPPX_MCP_PROBE_RETRY_BACKOFF_SECONDS` | `0.3`, range `0..5` | MCP probe retry backoff | Tune probe retry pacing |

MCP server config also supports long-task proxy fields:

- `longTaskProxy` / `long_task_proxy`: defaults to `true`. When enabled, openppx wraps ADK MCP tools and uses `inlineBudgetMs` to choose inline return vs. `TaskRun`.
- `inlineBudgetMs` / `inline_budget_ms`: defaults to `5000` and is clamped by runtime safety bounds.
- `jobProtocol` / `job_protocol`: optional. Declares the external job protocol for that MCP server. openppx converts MCP calls that return `job_id` into pollable external `TaskRun`s only when this is configured.

Minimal `jobProtocol` example:

```json
{
  "tools": {
    "mcpServers": {
      "remote": {
        "url": "https://example.com/mcp",
        "jobProtocol": {
          "jobIdPath": "job_id",
          "statusTool": "job_status",
          "statusArgs": {"job_id": "{job_id}"},
          "outputTool": "job_output",
          "cancelTool": "job_cancel",
          "pollTimeoutMs": 5000
        }
      }
    }
  }
}
```

`jobProtocol` does not predict whether a call will run for a long time. It only tells openppx how to query status, read output, and request cancellation after a tool result already returned a job id. If `cancelTool` is missing, UI/API surfaces do not offer `cancel_task`.

### WhatsApp Bridge and Runtime Switches

| Variable | Default | Purpose | When to set it |
|---|---|---|---|
| `WHATSAPP_BRIDGE_URL` | empty, config usually uses `ws://localhost:3001` | WhatsApp bridge WebSocket URL | WhatsApp channel is enabled |
| `WHATSAPP_BRIDGE_TOKEN` | empty | WhatsApp bridge auth token | Bridge token auth is enabled |
| `OPENPPX_WHATSAPP_BRIDGE_PRECHECK` | `1` | Gateway/doctor prechecks bridge reachability | Skip local debug prechecks |
| `OPENPPX_WHATSAPP_BRIDGE_SOURCE` | empty | Bridge source directory containing `package.json` | Bridge resources are outside the default location |
| `OPENPPX_SUBAGENT_MAX_CONCURRENCY` | `2`, range `1..16` | Subagent concurrency limit | Tune throughput or resource usage |
| `OPENPPX_DEBUG_MAX_CHARS` | `2000`, range `200..20000` | Maximum characters per debug log text segment | Debug long prompt truncation |

### GUI Automation Actions and Task Orchestration

GUI automation should generally be exposed through the MCP GUI service. Legacy built-in `computer_use` / `computer_task` remain available as fallback. Built-in GUI tools enter the sync tool proxy: calls that finish within the inline budget return in the old format, while longer calls are exposed as `TaskRun` and continue in the current process background.

| Variable | Default | Purpose | When to set it |
|---|---|---|---|
| `OPENPPX_GUI_MODEL` | empty | Grounding model for `computer_use` | Required for single-step GUI tools |
| `OPENPPX_GUI_BASE_URL` | empty | Grounding model API base URL | Use a compatible gateway or private deployment |
| `OPENPPX_GUI_PLANNER_MODEL` | empty, falls back to `OPENPPX_GUI_MODEL` | Multi-step planner model for `computer_task` | Recommended for multi-step GUI tasks |
| `OPENPPX_GUI_PLANNER_BASE_URL` | empty, falls back to GUI base URL | Planner API base URL | Planner and executor use different gateways |
| `OPENPPX_GUI_GROUNDING_PROVIDER` | empty | Grounding provider name such as `google` or `openai` | Resolve API keys by provider env mapping |
| `OPENPPX_GUI_PLANNER_PROVIDER` | empty, falls back to grounding provider | Planner provider name | Planner and grounding providers differ |
| `OPENPPX_GUI_MAX_PARSE_RETRIES` | `1` | Retry count for parsing `computer_use` model output | Model output is unstable |
| `OPENPPX_GUI_MAX_ACTION_RETRIES` | `1` | Retry count when a GUI action causes no screen change | GUI response is occasionally slow |
| `OPENPPX_GUI_VERIFY_SCREEN_CHANGE` | `true` | Verify screenshot changes before/after actions | Temporarily disable during debugging |
| `OPENPPX_GUI_MAX_WAIT_SECONDS` | `5.0` | Maximum wait action duration | Tasks need longer waits |
| `OPENPPX_GUI_ALLOW_DANGEROUS_KEYS` | `false` | Allow dangerous key combinations | Default should stay `false` |
| `OPENPPX_GUI_ALLOWED_ACTIONS` | empty | Comma-separated action allowlist | Restrict the execution surface |
| `OPENPPX_GUI_BLOCKED_ACTIONS` | empty | Comma-separated action denylist | Block specific actions |
| `OPENPPX_GUI_TASK_MAX_STEPS` | `8` | Maximum `computer_task` step count | Tasks need more steps |
| `OPENPPX_GUI_TASK_PARSE_RETRIES` | `1` | Planner JSON parse retry count | Planner output is unstable |
| `OPENPPX_GUI_TASK_MAX_NO_PROGRESS_STEPS` | `3` | Consecutive no-progress threshold before `status_code=no_progress` | Prevent task spinning |
| `OPENPPX_GUI_TASK_MAX_REPEAT_ACTIONS` | `3` | Consecutive same-action threshold before `status_code=no_progress` | Prevent repeated action loops |
| `OPENPPX_SYNC_PROXY_INLINE_BUDGET_MS` | `5000`, range `0..120000` | Inline wait budget before exposing built-in sync tools as `TaskRun` | Tune longer-running sync tools |
| `OPENPPX_SYNC_PROXY_MAX_WORKERS` | `4`, range `1..32` | Sync proxy worker pool size | Control resource usage |

The sync proxy supervises only synchronous calls in the current process. Python threads cannot be safely force-killed by the framework, so the built-in GUI fallback does not expose generic `interrupt_task` / `cancel_task`. Prefer MCP/job-based GUI runners when cancellation and checkpointing are required.

Minimal GUI environment example:

```bash
export OPENPPX_GUI_MODEL=gpt-4.1-mini
export OPENPPX_GUI_PLANNER_MODEL=gpt-4.1-mini
export OPENPPX_GUI_GROUNDING_PROVIDER=openai
export OPENAI_API_KEY=your_api_key
```

Optional action policy example:

```bash
export OPENPPX_GUI_ALLOWED_ACTIONS=wait,left_click,double_click,type,key,scroll
export OPENPPX_GUI_BLOCKED_ACTIONS=right_click,left_click_drag
export OPENPPX_GUI_ALLOW_DANGEROUS_KEYS=false
```

## Configuration Example

```json
{
  "agent": {
    "workspace": "/path/to/agent-workspace",
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
      "verificationToken": "",
      "groupPolicy": "mention",
      "replyToMessage": false,
      "reactEmoji": "THUMBSUP",
      "streamingEnabled": false
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
      "openppx_gui": {
        "enabled": true,
        "command": "openppx-gui-mcp",
        "args": [],
        "toolNamePrefix": "mcp_gui",
        "requireConfirmation": true
      },
      "tenant_api": {
        "enabled": false,
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
  },
  "debug": false
}
```

## Platform Notes

### Feishu

If your environment uses a SOCKS proxy, Feishu WebSocket depends on `python-socks`, which is included in default dependencies.

### WhatsApp

WhatsApp Bridge requires Node.js `>=20`. Runtime files live under `~/.openppx/bridge/`.
