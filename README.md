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

Command discovery (`--help` first):

```bash
openheron --help
openheron install --help
openheron gateway --help
openheron gateway-service --help
openheron gateway-service install --help
openheron provider --help
openheron channels --help
openheron cron --help
openheron token --help
```

Install smoke script:

```bash
scripts/install_smoke.sh
scripts/install_smoke.sh --with-gateway
```

Install examples:

```bash
openheron install
openheron install --init-only
openheron install --non-interactive --accept-risk
openheron install --install-daemon --daemon-channels local,feishu
```

Gateway and gateway-service:

- `openheron gateway`: run gateway runtime itself (foreground or background process management).
- `openheron gateway-service`: manage OS user-service manifest (launchd/systemd) that runs `openheron gateway`.

Minimal examples:

```bash
openheron gateway --channels local,feishu --interactive-local
openheron gateway status
openheron gateway-service install --channels local,feishu --enable
openheron gateway-service status
```

Background runtime/log files are stored under:

- `~/.openheron/log/gateway.pid`
- `~/.openheron/log/gateway.meta.json`
- `~/.openheron/log/gateway.out.log`
- `~/.openheron/log/gateway.err.log`
- `~/.openheron/log/gateway.debug.log`
- `~/.openheron/token_usage.db` (LLM token usage events)

Install output highlights:

- `Install summary: provider=..., channels=...`: active provider/channel selection
- `Install summary: missing=[...]`: key fields still missing for enabled components
- `Install summary: fixes=[...]`: direct config fix hints (`~/.openheron/global_config.json`)
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

- `~/.openheron/global_config.json`
- `~/.openheron/global_runtime.json`
- `~/.openheron/agents/<agentId>/config.json` (per-agent runtime policy/config)
- `~/.openheron/agents/main/` (including `workspace/`, `bootstrap/`, `sessions/`, `memory/`, `runtime/`)

Use `openheron install` for the full guided setup (checks + summary + suggestions),
and use `openheron install --init-only` when you only want minimal file initialization.

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
`global_config.json`, `global_runtime.json`, `agents/<agentId>/config.json`, workspace, logs, and runtime state files).

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

# multi-channel gateway runtime
openheron gateway --channels local,feishu --interactive-local
openheron gateway-service install --channels local,feishu --enable
openheron gateway-service status
openheron doctor
openheron heartbeat status
openheron heartbeat status --agent-id main
openheron token stats --provider google --limit 50
openheron token stats --agent-id main --limit 50
openheron token stats --provider google --agent-id biz --last-hours 24
openheron token stats --since 2026-02-26T00:00:00+08:00 --until 2026-02-26T23:59:59+08:00
openheron token stats --last-hours 24
```

For full subcommand options, use the `--help` entries in "Command discovery" above.

## Core Capabilities

- Runtime: Google ADK (`LlmAgent` + tools + callbacks)
- Session: SQLite-backed ADK session service
- Memory backends: `in_memory` / `markdown`
- Context compaction: ADK `EventsCompactionConfig`
- Slash commands: `/help` and `/new`
- Channel bridge: local + mainstream chat connectors
- Desktop GUI automation tools: `computer_use` (single-step) / `computer_task` (multi-step)

## GUI Automation Quick Start

`openheron` now includes two desktop GUI tools:

- `computer_use(action=...)`: one-step GUI grounding and execution
- `computer_task(task=..., max_steps=...)`: planner + multi-step GUI loop

Recommended minimal environment:

```bash
export OPENHERON_GUI_MODEL=gpt-4.1-mini
export OPENHERON_GUI_PLANNER_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=your_api_key
```

Smoke script examples:

```bash
# single-step (real execution)
./.venv/bin/python scripts/gui_smoke.py --mode single --action "等待 1 秒"

# multi-step (dry-run)
./.venv/bin/python scripts/gui_smoke.py --mode task --task "打开浏览器并搜索 openheron" --max-steps 8 --dry-run
```

macOS permission reminder (required for GUI automation):

- `Privacy & Security -> Screen Recording` (Terminal / Python host process)
- `Privacy & Security -> Accessibility` (for keyboard/mouse control)

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
