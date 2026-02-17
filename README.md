# sentientagent_v2

`sentientagent_v2` is a lightweight, skills-first agent built with Google ADK, focused on learning and education use cases.

Compared to nanobot, sentientagent_v2 is intentionally smaller and simpler.
You can think of sentientagent_v2 as a "Hello World" edition of the OpenClaw-style agent workflow.

## Scope

- Keeps: local skill discovery and loading (`SKILL.md`)
- Adds: minimal bus/channel gateway with pluggable channels (`local`, `feishu`)
- Runtime: Google ADK (`LlmAgent` + function tools)
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
- `cron` (local persisted add/list/remove)

## Installation

```bash
cd sentientagent_v2
pip install -e .
```

If you see `the greenlet library is required to use this function`, install:

```bash
pip install greenlet
```

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

- `providers.google.enabled / apiKey / model`
- `channels.local.enabled`, `channels.feishu.enabled`, and `channels.feishu.*`
- `web.enabled`, `web.search.enabled / provider / apiKey / maxResults`
- `security.strictMode / restrictToWorkspace / allowExec / allowNetwork / execAllowlist`

Use env vars only for temporary overrides, for example:

- `GOOGLE_API_KEY`
- `SENTIENTAGENT_V2_CHANNELS`
- `SENTIENTAGENT_V2_STRICT_MODE`
- `SENTIENTAGENT_V2_EXEC_ALLOWLIST`
- `SENTIENTAGENT_V2_DEBUG`

## Feishu Dependency

Install Feishu SDK only when needed:

```bash
pip install -e '.[feishu]'
```

If your environment uses a SOCKS proxy and you see
`python-socks is required to use a SOCKS proxy`, install:

```bash
pip install python-socks
```

## Config Example

```json
{
  "agent": {
    "workspace": "~/.sentientagent_v2/workspace",
    "builtinSkillsDir": ""
  },
  "providers": {
    "active": "google",
    "google": {
      "enabled": true,
      "apiKey": "your_google_api_key",
      "model": "gemini-3-flash-preview"
    },
    "openai": {
      "enabled": false,
      "apiKey": "",
      "model": ""
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
    "strictMode": false,
    "restrictToWorkspace": false,
    "allowExec": true,
    "allowNetwork": true,
    "execAllowlist": []
  },
  "debug": false
}
```

`session` always uses SQLite. If `dbUrl` is empty, the default path is
`~/.sentientagent_v2/database/sessions.db`.

## Security Policy

`sentientagent_v2` applies one unified security policy to file tools, shell execution, and web tools.

- `restrictToWorkspace=true`: file read/write/edit/list and shell path arguments are constrained to the workspace root.
- `strictMode=true`: forces workspace restriction, and disables exec/network by default unless explicitly re-enabled.
- `execAllowlist`: optional command-name allowlist for `exec`.

## Acknowledgements

This project is inspired by and partially adapted from [nanobot](https://github.com/HKUDS/nanobot).
Some implementation patterns and skill-related resources are derived from that project.
