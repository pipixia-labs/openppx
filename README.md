<div align="center">
 <img src="assets/openpipixia_logo_3.png" alt="openpipixia" width="500">
  <h1>OpenPipixia: A Lightweight Personal AI Assistant 🚀</h1>
</div>

## ✨ News

- 2026-02-18: V0.2 released with multi-agent and GUI operation support.

- 2026-02-12: Initial version released with single-agent support, including Feishu image and file sending/receiving.

## 🔧 Key Features

- Multi-agent support and compatibility with common providers.
- Agents can operate the OS with computer-use tools.


## 🧭 Quick Start

### 🛠️ 1. Set Up the Environment and Create an Agent
```bash
git clone https://github.com/pipixia-labs/openpipixia
cd openpipixia
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt 
pip install .
ppx create --name "assistant-main"
# Follow the `ppx create` output and edit the generated config files.
```

`ppx create` creates one role-based agent at a time. By default:

- the role is `Assistant` (low-privilege)
- the workspace is a new directory under the system temp directory
- the new agent is added to and enabled in `~/.openpipixia/global_config.json`

Example agent files:

- `~/.openpipixia/assistant-main/config.json`
- `~/.openpipixia/assistant-main/runtime.json`
- `~/.openpipixia/global_config.json`

You can also create higher-privilege agents explicitly:

```bash
ppx create --name "operator-main" --role operator
ppx create --name "manager-main" --role manager --workspace ~/work/openppx-manager
```

Each agent has a per-agent config home under `~/.openpipixia/<agent_name>/` that includes:

- `AGENTS.md`, `SOUL.md`, `TOOLS.md`, `IDENTITY.md`, `USER.md`
- `HEARTBEAT.md`
- `skills/`
- `memory/MEMORY.md`, `memory/HISTORY.md`

The `workspace` is separate and is only for code, task outputs, temporary files, and other working artifacts.

### 🔑 2. Configure Provider Keys

Review and edit your configuration files:

- `global_config.json`
- Each agent's config/runtime/agent-home files, for example:
  `~/.openpipixia/assistant-main/config.json`

Fill in required provider keys and assign per-agent security settings.
You can leave channel-specific keys (for example Telegram, Feishu, Weixin, or WeCom) empty at this stage.

Important:

- `ppx create` only creates and enables an agent. It does not automatically turn on Feishu, Telegram, or other channels.
- Channel settings must be edited in the `config.json` of the agent that is actually enabled and running.
- If you created new agents such as `assistant-main` / `operator-main` / `manager-main`, but only updated old agent configs like `agent_name_1`, gateway will not use those old channel settings.
- Before troubleshooting a Feishu connection issue, first run `ppx list` and confirm which agent is enabled, then check that agent's `channels.feishu.enabled`, `appId`, and `appSecret`.

### 💬 3. Try Local Interactive Mode

```bash
ppx --config-path ~/.openpipixia/assistant-main/config.json gateway run --channels local --interactive-local
```

### 🛰️ 4. Enable Channel Chat and Start Background Service

For channel keys and secrets, see [`docs/CHANNELS.md`](./docs/CHANNELS.md). After filling in channel keys, start the background gateway for regular usage:

Example for Feishu: if `assistant-main` is the agent you want to connect to Feishu, edit `~/.openpipixia/assistant-main/config.json` and set:

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx"
    }
  }
}
```

Do not only update another disabled agent's config, or gateway will still fail to connect that channel for your active agent.

```bash
ppx gateway start
```



## 🧪 Command Discovery

```bash
ppx --help
ppx list
ppx enable assistant-main
ppx disable assistant-main
ppx delete assistant-main
ppx gateway --help
ppx gateway-service --help
ppx provider --help
ppx channels --help
ppx cron --help
ppx heartbeat --help
ppx token --help
```

## 🌉 Gateway Usage

- `ppx gateway run`: run the gateway in the foreground
- `ppx gateway start|stop|restart|status`: start, stop, restart, and inspect the background gateway process
- `ppx gateway-service`: manage OS user-service manifests (launchd/systemd)

Examples:

```bash
ppx gateway run --channels local,feishu --interactive-local
ppx gateway status
ppx gateway-service install --channels local,feishu --enable
ppx gateway-service status
```

Weixin login helper:

```bash
ppx channels login weixin
ppx gateway run --channels local,weixin --interactive-local
```

WeCom optional dependency:

```bash
pip install -e .[wecom]
```

Weixin optional dependency for QR/media support:

```bash
pip install -e .[weixin]
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
ppx list
ppx enable operator-main
ppx disable assistant-main
ppx delete assistant-main

# Single-turn call
python -m openpipixia.cli -m "Describe what you can do"
python -m openpipixia.cli -m "Describe what you can do" --user-id local --session-id demo001

# Local interactive gateway
python -m openpipixia.cli gateway run --channels local --interactive-local

# Multi-channel runtime
ppx gateway run --channels local,feishu --interactive-local
ppx gateway-service install --channels local,feishu --enable
ppx gateway-service status
ppx doctor
ppx heartbeat status
ppx token stats --provider google --limit 50
ppx token stats --last-hours 24
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
ppx doctor --fix --json
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

Only run this cleanup if you no longer need existing config, agent-home files, workspaces, logs, or local runtime records.
