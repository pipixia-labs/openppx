# openheron

`openheron` is a lightweight, skills-first agent runtime built on Google ADK.

It focuses on:

- Multi-channel gateway execution
- Local skill loading (`SKILL.md`)
- Built-in action tools (file/shell/web/message/cron/subagent)
- Persistent session + optional long-term memory

Compared with larger systems, this project keeps the core runtime compact and easy to iterate.

## Prerequisites

- Python 3.14
- A virtual environment is strongly recommended (examples below use `.venv`)

## Quick Start

```bash
cd openheron_root
python3.14 -m venv .venv
source .venv/bin/activate
pip install .
openheron install
python -m openheron.cli -m "Describe what you can do"
```

`openheron install` now includes:

- config/workspace initialization
- optional interactive provider/channel setup
- guided missing-field review for enabled provider/channels (interactive mode)
- diagnostics (`openheron doctor`)
- install summary + next command suggestions

Install command variants:

```bash
openheron install
openheron install --init-only
openheron install --non-interactive --accept-risk
openheron install --force
openheron install --install-daemon
openheron install --install-daemon --daemon-channels local,feishu
```

Install smoke script:

```bash
scripts/install_smoke.sh
scripts/install_smoke.sh --with-gateway
```

Gateway service manifest commands:

```bash
openheron gateway-service install
openheron gateway-service install --force --channels local,feishu
openheron gateway-service install --enable
openheron gateway-service status
```

Gateway background service commands:

```bash
openheron gateway start --channels local,feishu
openheron gateway status
openheron gateway restart --channels local,feishu
openheron gateway stop
```

Background runtime/log files are stored under:

- `~/.openheron/log/gateway.pid`
- `~/.openheron/log/gateway.meta.json`
- `~/.openheron/log/gateway.out.log`
- `~/.openheron/log/gateway.err.log`
- `~/.openheron/log/gateway.debug.log`

Install output highlights:

- `Install summary: provider=..., channels=...`: active provider/channel selection
- `Install summary: missing=[...]`: key fields still missing for enabled components
- `Install summary: fixes=[...]`: direct config fix hints (`~/.openheron/config.json`)
- `Install summary: next[1]/next[2]`: recommended follow-up commands
- `Install prereq: ...`: local prerequisite checks (`.venv`, `adk`, optional `questionary/rich`)
  (`doctor` text mode renders them as `Install prereq [ok]` / `Install prereq [warn]`)

Typical `missing` entries include provider API key plus channel credentials
(feishu/telegram/discord/dingtalk/slack/whatsapp/mochat/email/qq).  
See [`docs/OPERATIONS.md`](./docs/OPERATIONS.md) for the full field-to-fix mapping.

If you only want file initialization without checks, run:

```bash
openheron install --init-only
```

`openheron install --init-only` initializes:

- `~/.openheron/config.json`
- `~/.openheron/runtime.json`
- `~/.openheron/workspace`

Use `openheron install` for the full guided setup (checks + summary + suggestions),
and use `openheron install --init-only` when you only want minimal file initialization.
`openheron onboard` is kept as a compatibility alias.

## Development

Install in editable mode:

```bash
cd openheron_root
source .venv/bin/activate
pip install -e .
```

Run tests during development:

```bash
pytest -q
```

Uninstall (run inside the same Python environment where openheron was installed):

```bash
pip uninstall openheron
```

`pip uninstall openheron` only removes the Python package/CLI entrypoint.
It does not delete user data under `~/.openheron/` (for example
`config.json`, `runtime.json`, workspace, logs, and runtime state files).

If you also want to remove personalized/local runtime data, delete it manually:

```bash
rm -rf ~/.openheron
```

Run this cleanup only if you are sure you no longer need existing config,
workspace files, logs, or local runtime records.

## Quick Ops Summary (from `docs/OPERATIONS.md`)

```bash
# single-turn call
python -m openheron.cli -m "Describe what you can do"
python -m openheron.cli -m "Describe what you can do" --user-id local --session-id demo001

# local gateway
python -m openheron.cli gateway-local

# multi-channel gateway
openheron gateway --channels local,feishu --interactive-local
export OPENHERON_CHANNELS=feishu
openheron gateway

# diagnostics and providers
openheron doctor
openheron doctor --fix
openheron doctor --fix-dry-run
openheron heartbeat status
openheron gateway-service status
openheron skills
openheron provider list
openheron provider status
openheron provider login github-copilot
openheron provider login openai-codex
```

WhatsApp bridge quick flow:

```bash
openheron channels login
openheron channels bridge start
openheron channels bridge status
openheron channels bridge stop
scripts/whatsapp_bridge_e2e.sh smoke
```

Cron quick flow (jobs run only while gateway is running):

```bash
openheron cron list
openheron cron add --name daily --message "daily report" --cron "0 9 * * 1-5" --tz Asia/Shanghai
openheron cron status
```

## Common Commands

```bash
# local gateway
python -m openheron.cli gateway-local

# multi-channel gateway
openheron gateway --channels local,feishu --interactive-local

# diagnostics
openheron doctor
openheron skills
```

## Core Capabilities

- Runtime: Google ADK (`LlmAgent` + tools + callbacks)
- Session: SQLite-backed ADK session service
- Memory backends: `in_memory` / `markdown`
- Context compaction: ADK `EventsCompactionConfig`
- Slash commands: `/help` and `/new`
- Channel bridge: local + mainstream chat connectors

## Project Layout

```text
openheron_root/
├── README.md
├── docs/
├── openheron/
├── tests/
└── scripts/
```

## Documentation

Detailed docs are in [`docs/`](./docs/):

- [`docs/PROJECT_OVERVIEW.md`](./docs/PROJECT_OVERVIEW.md)
- [`docs/OPERATIONS.md`](./docs/OPERATIONS.md)
- [`docs/CONFIGURATION.md`](./docs/CONFIGURATION.md)
- [`docs/MCP_SECURITY.md`](./docs/MCP_SECURITY.md)
- [`docs/README.md`](./docs/README.md)

Recommended reading order: start with `OPERATIONS.md` (runtime and commands),
then `CONFIGURATION.md` (settings and env mapping), then topic-specific docs as needed.

Install troubleshooting tips are in `docs/OPERATIONS.md` under
`install 常见问题`.
When `openheron install` reports missing setup, prioritize the
`Install summary: fixes=[...]` hints first.
If you consume doctor results programmatically, use
`openheron doctor --fix --json` and read
`fix.reasonCodes` / `fix.byRule` (see `docs/OPERATIONS.md` for examples).

## Testing

```bash
source .venv/bin/activate
pytest -q
```
