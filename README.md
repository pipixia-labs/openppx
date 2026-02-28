<div align="center">
  <h1>OpenHeron: A Lightweight Personal AI Assistant</h1>
</div>



## Overview

`openheron` is designed to be practical and easy to iterate.

Core focus areas:

- Multi-channel gateway runtime
- Local skill loading from `SKILL.md`
- Built-in tools (file/shell/web/message/cron/subagent)
- Persistent sessions with optional long-term memory
- Multi-agent and multi-provider support

## Prerequisites

- Python 3.14
- A virtual environment is strongly recommended (examples below use `.venv`)

## Quick Start

```bash
cd openheron_root
python3.14 -m venv .venv
source .venv/bin/activate
pip install .
openheron init
openheron doctor
python -m openheron.cli -m "Describe what you can do"
```

## What `openheron init` Creates

`openheron init` scaffolds a default multi-agent setup:

- `~/.openheron/agent_name_1`
- `~/.openheron/agent_name_2`
- `~/.openheron/agent_name_3`
- `~/.openheron/global_config.json`

By default, only `agent_name_1` is enabled in `global_config.json`.

Each agent workspace includes bootstrap/task files and local scaffolding, including:

- `AGENTS.md`, `SOUL.md`, `TOOLS.md`, `IDENTITY.md`, `USER.md`
- `HEARTBEAT.md`
- `skills/`
- `memory/MEMORY.md`, `memory/HISTORY.md`

## First-Run Workflow

1. Review and edit your configuration files.
- `global_config.json`
- Each agent's config/runtime/workspace files

2. Validate readiness:

```bash
openheron doctor
```

3. Try local interactive mode first:

```bash
openheron --config-path ~/.openheron/agent_name_1/config.json gateway run --channels local --interactive-local
```

4. Start background gateway for regular usage:

```bash
openheron gateway start
```

## Command Discovery

```bash
openheron --help
openheron gateway --help
openheron gateway-service --help
openheron provider --help
openheron channels --help
openheron cron --help
openheron heartbeat --help
openheron token --help
```

## Gateway Usage

- `openheron gateway run`: run the gateway in foreground
- `openheron gateway start|stop|restart|status`: manage the background gateway process
- `openheron gateway-service`: manage OS user-service manifests (launchd/systemd)

Examples:

```bash
openheron gateway run --channels local,feishu --interactive-local
openheron gateway status
openheron gateway-service install --channels local,feishu --enable
openheron gateway-service status
```

## GUI Automation

`openheron` includes desktop GUI tools. And you need to edit the `config.json` or set the following environment variables:

```bash
export OPENHERON_GUI_MODEL=$NAME_OF_YOUR_MLLM
export OPENHERON_GUI_PLANNER_MODEL=$NAME_OF_YOUR_MLLM
export OPENAI_API_KEY=your_api_key
```

- `OPENHERON_GUI_MODEL`: multi-modal model used for low-level GUI grounding/actions
- `OPENHERON_GUI_PLANNER_MODEL`: multi-modal model used for multi-step planning
- `OPENAI_API_KEY`: API key for the selected OpenAI-compatible provider

GUI smoke examples:

```bash
# Single-step (real execution)
./.venv/bin/python scripts/gui_smoke.py --mode single --action "Wait 1 second"

# Multi-step (dry run)
./.venv/bin/python scripts/gui_smoke.py --mode task --task "Open a browser and search for openheron" --max-steps 8 --dry-run
```

macOS permission reminder (required for GUI automation):

- `Privacy & Security -> Screen Recording` (Terminal / Python host process)
- `Privacy & Security -> Accessibility` (keyboard/mouse control)

## Runtime Files

Background runtime/log files:

- `~/.openheron/log/gateway.pid`
- `~/.openheron/log/gateway.meta.json`
- `~/.openheron/log/gateway.out.log`
- `~/.openheron/log/gateway.err.log`
- `~/.openheron/log/gateway.debug.log`
- `~/.openheron/token_usage.db` (LLM token usage events)

Workspace-level runtime state lives under `<workspace>/.openheron/`
(for example cron and heartbeat runtime snapshots).

## Development

Install in editable mode:

```bash
cd openheron_root
source .venv/bin/activate
pip install -e .
```

Run tests:

```bash
pytest -q
```

Developer smoke checks:

```bash
scripts/install_smoke.sh
scripts/install_smoke.sh --with-gateway
```

## Quick Ops

```bash
# Single-turn call
python -m openheron.cli -m "Describe what you can do"
python -m openheron.cli -m "Describe what you can do" --user-id local --session-id demo001

# Local interactive gateway
python -m openheron.cli gateway run --channels local --interactive-local

# Multi-channel runtime
openheron gateway run --channels local,feishu --interactive-local
openheron gateway-service install --channels local,feishu --enable
openheron gateway-service status
openheron doctor
openheron heartbeat status
openheron token stats --provider google --limit 50
openheron token stats --last-hours 24
```

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

Detailed documentation is in [`docs/`](./docs/):

- [`docs/PROJECT_OVERVIEW.md`](./docs/PROJECT_OVERVIEW.md)
- [`docs/OPERATIONS.md`](./docs/OPERATIONS.md)
- [`docs/CONFIGURATION.md`](./docs/CONFIGURATION.md)
- [`docs/MCP_SECURITY.md`](./docs/MCP_SECURITY.md)
- [`docs/README.md`](./docs/README.md)

Recommended reading order:

1. `OPERATIONS.md` (runtime and commands)
2. `CONFIGURATION.md` (settings and environment mapping)
3. Topic-specific docs as needed

For programmatic doctor output:

```bash
openheron doctor --fix --json
```

Then inspect `fix.reasonCodes` and `fix.byRule`
(see `docs/OPERATIONS.md` for details).

## Uninstall

Run this in the same Python environment where `openheron` was installed:

```bash
pip uninstall openheron
```

This removes only the Python package and CLI entrypoint.
It does **not** remove user data under `~/.openheron/`.

To remove local runtime data as well:

```bash
rm -rf ~/.openheron
```

Only run this cleanup if you no longer need existing config, workspace files, logs, or local runtime records.
