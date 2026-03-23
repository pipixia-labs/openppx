<div align="center">
 <img src="assets/openpipixia_logo_2.png" alt="openpipixia" width="500">
  <h1>OpenPipixia: A Lightweight Personal AI Assistant 🚀</h1>
</div>

## ✨ News

- 2026-02-18: V0.2 released with multi-agent and GUI operation support.

- 2026-02-12: Initial version released with single-agent support, including Feishu image and file sending/receiving.

## 🔧 Key Features

- Multi-agent support and compatibility with common providers.
- Agents can operate the OS with computer-use tools.


## 🧭 Quick Start

### 🛠️ 1. Set Up the Environment and Initialize
```bash
git clone https://github.com/pipixia-labs/openpipixia
cd openpipixia
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt 
pip install .
openpipixia init
# Follow the `openpipixia init` output and edit the generated config files.
```

`openpipixia init` scaffolds a default multi-agent setup:

- `~/.openpipixia/agent_name_1`
- `~/.openpipixia/agent_name_2`
- `~/.openpipixia/agent_name_3`
- `~/.openpipixia/global_config.json`

By default, only `agent_name_1` is enabled in `global_config.json`.

Each agent workspace includes bootstrap/task files and local scaffolding, including:

- `AGENTS.md`, `SOUL.md`, `TOOLS.md`, `IDENTITY.md`, `USER.md`
- `HEARTBEAT.md`
- `skills/`
- `memory/MEMORY.md`, `memory/HISTORY.md`

### 🔑 2. Configure Provider Keys

Review and edit your configuration files:

- `global_config.json`
- Each agent's config/runtime/workspace files, for example:
  `~/.openpipixia/agent_name_1/config.json`

Fill in required provider keys and assign per-agent security settings.
You can leave channel-specific keys (for example Telegram or Feishu) empty at this stage.

### 💬 3. Try Local Interactive Mode

```bash
openpipixia --config-path ~/.openpipixia/agent_name_1/config.json gateway run --channels local --interactive-local
```

### 🛰️ 4. Enable Channel Chat and Start Background Service

For channel keys and secrets, see [`docs/CHANNELS.md`](./docs/CHANNELS.md). After filling in channel keys, start the background gateway for regular usage:

```bash
openpipixia gateway start
```



## 🧪 Command Discovery

```bash
openpipixia --help
openpipixia gateway --help
openpipixia gateway-service --help
openpipixia provider --help
openpipixia channels --help
openpipixia cron --help
openpipixia heartbeat --help
openpipixia token --help
```

## 🌉 Gateway Usage

- `openpipixia gateway run`: run the gateway in the foreground
- `openpipixia gateway start|stop|restart|status`: start, stop, restart, and inspect the background gateway process
- `openpipixia gateway-service`: manage OS user-service manifests (launchd/systemd)

Examples:

```bash
openpipixia gateway run --channels local,feishu --interactive-local
openpipixia gateway status
openpipixia gateway-service install --channels local,feishu --enable
openpipixia gateway-service status
```

## 🖥️ Computer Use

`openpipixia` includes desktop GUI tools.
Recommended: configure GUI models/providers in `config.json` (`multimodalProviders`, `gui.groundingProvider`, `gui.plannerProvider`).

Minimal `config.json` example:

```json
{
  "multimodalProviders": {
    "grounding_mm": {
      "enabled": true,
      "provider": "openai",
      "apiKey": "your_openai_key",
      "model": "gpt-5.2"
    },
    "planner_mm": {
      "enabled": true,
      "provider": "google",
      "apiKey": "your_gemini_key",
      "model": "gemini-3-flash-preview"
    }
  },
  "gui": {
    "groundingProvider": "grounding_mm",
    "plannerProvider": "planner_mm"
  }
}
```


GUI smoke examples:

```bash
# Single-step (real execution)
./.venv/bin/python scripts/gui_smoke.py --mode single --action "Wait 1 second"

# Multi-step (dry run)
./.venv/bin/python scripts/gui_smoke.py --mode task --task "Open a browser and search for openpipixia" --max-steps 8 --dry-run
```

macOS permission reminder (required for GUI automation):

- `Privacy & Security -> Screen Recording` (Terminal / Python host process)
- `Privacy & Security -> Accessibility` (keyboard/mouse control)

## 📂 Runtime Files

Background runtime/log files:

- `~/.openpipixia/log/gateway.pid`
- `~/.openpipixia/log/gateway.meta.json`
- `~/.openpipixia/log/gateway.out.log`
- `~/.openpipixia/log/gateway.err.log`
- `~/.openpipixia/log/gateway.debug.log`
- `~/.openpipixia/token_usage.db` (LLM token usage events)

Workspace-level runtime state lives under `<workspace>/.openpipixia/`
(for example cron and heartbeat runtime snapshots).

## 🧰 Development

Install in editable mode:

```bash
cd openppx_root
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

## ⚡ Quick Ops

```bash
# Single-turn call
python -m openpipixia.cli -m "Describe what you can do"
python -m openpipixia.cli -m "Describe what you can do" --user-id local --session-id demo001

# Local interactive gateway
python -m openpipixia.cli gateway run --channels local --interactive-local

# Multi-channel runtime
openpipixia gateway run --channels local,feishu --interactive-local
openpipixia gateway-service install --channels local,feishu --enable
openpipixia gateway-service status
openpipixia doctor
openpipixia heartbeat status
openpipixia token stats --provider google --limit 50
openpipixia token stats --last-hours 24
```

## 🗂️ Project Layout

```text
openppx_root/
├── README.md
├── assets/
├── docs/
│   ├── CONFIGURATION.md
│   ├── MCP_SECURITY.md
│   ├── OPERATIONS.md
│   ├── PROJECT_OVERVIEW.md
│   └── README.md
├── openpipixia/
│   ├── app/
│   ├── bridge/
│   ├── browser/
│   ├── bus/
│   ├── channels/
│   ├── core/
│   ├── gui/
│   ├── mcps/
│   ├── runtime/
│   ├── skills/
│   └── tooling/
├── scripts/
├── tests/
└── workspace/
```

## 📚 Documentation

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
openpipixia doctor --fix --json
```

Then inspect `fix.reasonCodes` and `fix.byRule`
(see `docs/OPERATIONS.md` for details).

## 🧹 Uninstall

Run this in the same Python environment where `openpipixia` was installed:

```bash
pip uninstall openpipixia
```

This removes only the Python package and CLI entrypoint.
It does **not** remove user data under `~/.openpipixia/`.

To remove local runtime data as well:

```bash
rm -rf ~/.openpipixia
```

Only run this cleanup if you no longer need existing config, workspace files, logs, or local runtime records.
