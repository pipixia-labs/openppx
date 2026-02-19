# sentientagent_v2

`sentientagent_v2` is a lightweight, skills-first agent built with Google ADK, focused on learning and education use cases.

Compared to nanobot, sentientagent_v2 is intentionally smaller and simpler.
You can think of sentientagent_v2 as a "Hello World" edition of the OpenClaw-style agent workflow.

## Scope

- Keeps: local skill discovery and loading (`SKILL.md`)
- Adds: minimal bus/channel gateway with pluggable channels (`local`, `feishu`, `telegram`, `whatsapp`, `discord`, `mochat`, `dingtalk`, `email`, `slack`, `qq`)
- Runtime: Google ADK (`LlmAgent` + function tools), with provider registry support:
  - native: `google`
  - LiteLLM: `openai`, `openrouter`, `anthropic`, `deepseek`, `groq`, `gemini`, `dashscope`, `zhipu`, `moonshot`, `minimax`, `aihubmix`, `siliconflow`, `vllm`, `custom`, `github_copilot` (OAuth)
  - ADK BaseLlm adapter (OAuth): `openai_codex`
- Bundles built-in skills under `sentientagent_v2/skills`
- Provides core tools for file, shell, web, messaging, and scheduling workflows

## Project Structure

```text
sentientagent_v2/
├── pyproject.toml
├── README.md
└── sentientagent_v2/
    ├── __init__.py
    ├── agent.py
    ├── cli.py
    ├── skills.py
    └── skills/
        └── general/
            └── SKILL.md
```

## Skill Model

`sentientagent_v2` discovers skills from:

1. `SENTIENTAGENT_V2_WORKSPACE/skills/*/SKILL.md` (workspace, higher priority)
2. Built-in `sentientagent_v2/skills/*/SKILL.md`

The agent exposes two skill tools:

- `list_skills()`: list available skills as JSON
- `read_skill(name)`: read full `SKILL.md` content

## Built-in Action Tools

- `read_file`, `write_file`, `edit_file`, `list_dir`
- `exec` (implemented by `exec_command`)
- `web_search`, `web_fetch`
- `message` (local outbox log)
- `message_image` (upload/send image on channels that support image messages, e.g. Feishu)
- `cron` (runtime scheduler + local persisted job store + delivery routing)

## Installation

```bash
cd sentientagent_v2
pip install -e .
```

This installs all runtime dependencies (Google ADK, Feishu SDK, LiteLLM/OpenAI).

## Onboard (Recommended)

Initialize local config and workspace:

```bash
sentientagent_v2 onboard
```

This creates:

- `~/.sentientagent_v2/config.json`
- `~/.sentientagent_v2/workspace`

Gateway/doctor/message commands will auto-load this config file and map it to runtime env vars.
For day-to-day use, update only `config.json` and avoid frequent manual `export` overrides.

## Run

### Single-turn request (recommended)

```bash
cd sentientagent_v2
python -m sentientagent_v2.cli -m "Describe what you can do"
```

You can also pass explicit identifiers:

```bash
python -m sentientagent_v2.cli -m "Describe what you can do" --user-id local --session-id demo001
```

### ADK CLI mode

```bash
adk run sentientagent_v2
```

### Wrapper CLI

```bash
sentientagent_v2 run
```

### Utilities

```bash
sentientagent_v2 skills
sentientagent_v2 doctor
sentientagent_v2 provider list
sentientagent_v2 provider status
sentientagent_v2 provider status --json
sentientagent_v2 provider login github-copilot
sentientagent_v2 provider login openai-codex
# Alias examples: codex -> openai-codex, copilot -> github-copilot
sentientagent_v2 provider login codex
sentientagent_v2 channels login
sentientagent_v2 channels bridge start
sentientagent_v2 channels bridge status
sentientagent_v2 channels bridge stop
```

### Gateway: local channel

```bash
python -m sentientagent_v2.cli gateway-local
```

### Gateway: channel mode (including Feishu)

```bash
sentientagent_v2 gateway --channels local,feishu --interactive-local
```

Or use env default:

```bash
export SENTIENTAGENT_V2_CHANNELS=feishu
sentientagent_v2 gateway
```

Recommended for Feishu: set channels and Feishu credentials in `~/.sentientagent_v2/config.json`,
then run:

```bash
sentientagent_v2 gateway
```

When users send file/image attachments in Feishu (for example PDF or image), `sentientagent_v2`
downloads them to `SENTIENTAGENT_V2_WORKSPACE/inbox/feishu/` and forwards local paths to the agent.

### WhatsApp QR Bridge (channels login helper)

`sentientagent_v2` uses a local Node.js WhatsApp bridge (Baileys + WebSocket) for channel login and runtime transport.

```bash
# foreground login helper (shows QR in current terminal)
sentientagent_v2 channels login

# optional background bridge lifecycle
sentientagent_v2 channels bridge start
sentientagent_v2 channels bridge status
sentientagent_v2 channels bridge stop
```

Notes:

- `channels login` starts bridge in foreground and is best for first-time QR scan.
- `channels bridge start` runs bridge in background and writes runtime state to `~/.sentientagent_v2/bridge/runtime_state.json`.
- `gateway` and `doctor` run a WhatsApp bridge precheck by default when `whatsapp` is enabled. Disable with `SENTIENTAGENT_V2_WHATSAPP_BRIDGE_PRECHECK=0`.
- If bridge source path needs override in dev mode, set `SENTIENTAGENT_V2_WHATSAPP_BRIDGE_SOURCE=/absolute/path/to/bridge`.

#### End-to-end quick check

```bash
# 1) one-command QR + smoke helper
scripts/whatsapp_bridge_e2e.sh full

# 2) or run smoke only (background start/status/doctor/stop)
scripts/whatsapp_bridge_e2e.sh smoke
```

## Cron Scheduler

`sentientagent_v2` cron is an in-process scheduler. It does not write to OS crontab.
Jobs are executed only while gateway is running.

- Store file: `SENTIENTAGENT_V2_WORKSPACE/.sentientagent_v2/cron_jobs.json`
- Supported schedules: `every`, `cron` (+`tz`), `at` (one-shot)
- Delivery loop: when `deliver=true`, execution output is pushed to configured channel/recipient
- Compatibility: legacy cron records are still readable and normalized at runtime

### CLI examples

```bash
# list jobs
sentientagent_v2 cron list

# add recurring job (every 5 minutes)
sentientagent_v2 cron add --name weather --message "check weather and summarize" --every 300

# add cron expression with timezone
sentientagent_v2 cron add --name daily --message "daily report" --cron "0 9 * * 1-5" --tz Asia/Shanghai

# add one-shot job
sentientagent_v2 cron add --name reminder --message "remind me to review PR" --at 2026-02-19T09:30:00

# enable outbound delivery
sentientagent_v2 cron add --name push --message "send update" --every 600 --deliver --channel feishu --to ou_xxx

# manual operations
sentientagent_v2 cron run <job_id>
sentientagent_v2 cron enable <job_id>
sentientagent_v2 cron enable <job_id> --disable
sentientagent_v2 cron remove <job_id>
sentientagent_v2 cron status
```

## Classic Usage Examples

```bash
python -m sentientagent_v2.cli -m "search for the latest research progress today, and create a PPT for me."
python -m sentientagent_v2.cli -m "download all PDF files from this page: https://bbs.kangaroo.study/forum.php?mod=viewthread&tid=467"
```

## Testing

```bash
source .venv/bin/activate
python -m pytest -q
```

## Environment Variables

`sentientagent_v2` supports both:

- config file: `~/.sentientagent_v2/config.json` (recommended)
- shell env vars (higher priority, overrides config values)

In normal usage, you do not need to set environment variables manually.
Configure these fields in `config.json`:

- `providers.<provider>.enabled / apiKey / model / apiBase / extraHeaders` (enable exactly one)
- `channels.<name>.enabled` and channel-specific fields (for example `channels.feishu.*`, `channels.whatsapp.*`)
- `web.enabled`, `web.search.enabled / provider / apiKey / maxResults`
- `security.restrictToWorkspace / allowExec / allowNetwork / execAllowlist`
- `tools.mcpServers` (optional MCP server map; supports stdio and remote HTTP/SSE)

Use env vars only for temporary overrides, for example:

- `GOOGLE_API_KEY`
- `OPENAI_API_KEY`
- `SENTIENTAGENT_V2_CHANNELS`
- `WHATSAPP_BRIDGE_URL`
- `WHATSAPP_BRIDGE_TOKEN`
- `SENTIENTAGENT_V2_WHATSAPP_BRIDGE_PRECHECK` (`1` by default)
- `SENTIENTAGENT_V2_WHATSAPP_BRIDGE_SOURCE` (optional bridge source override)
- `SENTIENTAGENT_V2_EXEC_ALLOWLIST`
- `SENTIENTAGENT_V2_MCP_SERVERS_JSON`
- `SENTIENTAGENT_V2_MCP_REQUIRED_SERVERS` (comma-separated strong dependencies for gateway startup)
- `SENTIENTAGENT_V2_MCP_PROBE_RETRY_ATTEMPTS` (MCP health-check retries, default `2`)
- `SENTIENTAGENT_V2_MCP_PROBE_RETRY_BACKOFF_SECONDS` (MCP retry backoff base, default `0.3`)
- `SENTIENTAGENT_V2_DEBUG`
- `SENTIENTAGENT_V2_DEBUG_MAX_CHARS` (default `2000`, max text length per debug message)

When `debug=true` (or `SENTIENTAGENT_V2_DEBUG=1`), `sentientagent_v2` emits callback-based LLM traces:

- `llm.before_model`: model name, tool names, and sanitized text sent to the model
- `llm.after_model`: finish reason, errors, and sanitized response text

## Feishu Note

If your environment uses a SOCKS proxy, Feishu websocket requires `python-socks`.
It is already included in default dependencies.

## WhatsApp Note

WhatsApp bridge helper requires Node.js (`npm`) >= 20. Runtime bridge files are managed under `~/.sentientagent_v2/bridge/`.

## Config Example

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
      },
      "docs": {
        "url": "https://example.com/mcp",
        "transport": "http",
        "headers": {
          "Authorization": "Bearer <token>"
        },
        "toolFilter": ["search_docs"],
        "toolNamePrefix": "mcp_docs_",
        "requireConfirmation": false
      }
    }
  },
  "debug": false
}
```

Provider selection is determined by `enabled` flags only. Keep exactly one provider enabled.
Runtime supports `google` plus all registry-listed LiteLLM providers.
`openai_codex` runs via a dedicated ADK `BaseLlm` adapter and requires OAuth login first.

`session` always uses SQLite. If `dbUrl` is empty, the default path is
`~/.sentientagent_v2/database/sessions.db`.

## MCP Tool Integration (Minimum Setup)

`sentientagent_v2` uses ADK `McpToolset` and reads server config from `tools.mcpServers`.

Per-server fields:

- `command` + `args` + `env`: stdio MCP server
- `url`: remote MCP server
- `transport`: optional, `sse` or `http` (auto-detects `/sse` as SSE when omitted)
- `headers`: optional HTTP headers for remote MCP
- `toolFilter` (or `tool_filter`): optional allowlist of MCP tool names
- `toolNamePrefix` (or `tool_name_prefix`): optional prefix for exposed tool names
- `requireConfirmation` (or `require_confirmation`): optional confirmation gate

Quick verify:

1. Add one server under `tools.mcpServers` in `~/.sentientagent_v2/config.json`.
2. Run `sentientagent_v2 doctor` to run MCP health checks (list tools per configured server).
3. Optional automation mode: `sentientagent_v2 doctor --json` (machine-readable), or `sentientagent_v2 doctor --verbose`.
4. Start gateway: `sentientagent_v2 gateway` (startup logs print MCP server summary).
5. Ask the agent to use MCP tools by prefix (for example `mcp_filesystem_...`).

Optional:

- `SENTIENTAGENT_V2_MCP_DOCTOR_TIMEOUT_SECONDS` controls per-server doctor MCP timeout (default `5`, range `1..30`).
- `SENTIENTAGENT_V2_MCP_GATEWAY_TIMEOUT_SECONDS` controls required-server MCP timeout at gateway startup (default `5`, range `1..30`).
- `SENTIENTAGENT_V2_MCP_PROBE_RETRY_ATTEMPTS` controls retries for transient MCP probe failures (default `2`, range `1..5`).
- `SENTIENTAGENT_V2_MCP_PROBE_RETRY_BACKOFF_SECONDS` controls retry backoff base seconds (default `0.3`, range `0..5`).
- `SENTIENTAGENT_V2_MCP_REQUIRED_SERVERS` can enforce strong MCP dependencies. If any required server is missing or unhealthy, `sentientagent_v2 gateway` exits before startup.

## Security Policy

`sentientagent_v2` applies one unified security policy to file tools, shell execution, and web tools.

| Field | Default | Meaning |
|-------|---------|---------|
| `restrictToWorkspace` | `false` | Restricts file tools (`read_file`, `write_file`, `edit_file`, `list_dir`) and shell path arguments to `SENTIENTAGENT_V2_WORKSPACE`. |
| `allowExec` | `true` | Enables/disables the `exec` tool entirely. If `false`, all `exec` calls are blocked. |
| `allowNetwork` | `true` | Enables/disables network tools (`web_search`, `web_fetch`). If `false`, network calls are blocked. |
| `execAllowlist` | `[]` | Optional command-name allowlist for `exec` (example: `["python", "git", "ls"]`). Empty means no allowlist restriction. |

Behavior notes:

- `execAllowlist` checks command name only (the first argv token after parsing).
- `exec` runs with `shell=False` for a safer default (no shell piping/chaining semantics by default).

## Acknowledgements

This project is inspired by and partially adapted from [nanobot](https://github.com/HKUDS/nanobot).
Some implementation patterns and skill-related resources are derived from that project.
