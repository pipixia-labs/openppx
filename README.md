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
python -m sentientagent_v2.cli gateway --channels local,feishu --interactive-local
```

Or use env default:

```bash
export SENTIENTAGENT_V2_CHANNELS=feishu
python -m sentientagent_v2.cli gateway
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

- `SENTIENTAGENT_V2_MODEL`: override model (default: `gemini-3-flash-preview`)
- `SENTIENTAGENT_V2_WORKSPACE`: workspace root for custom skills
- `SENTIENTAGENT_V2_BUILTIN_SKILLS_DIR`: override built-in skills directory
- `SENTIENTAGENT_V2_CHANNELS`: default channel list for `gateway` (e.g. `local,feishu`)
- `SENTIENTAGENT_V2_SESSION_BACKEND`: `memory` (default) or `sqlite`
- `SENTIENTAGENT_V2_SESSION_DB_URL`: DB URL when using sqlite backend (optional)
- `FEISHU_APP_ID`: required for Feishu channel
- `FEISHU_APP_SECRET`: required for Feishu channel
- `FEISHU_ENCRYPT_KEY`: optional for Feishu event decrypt
- `FEISHU_VERIFICATION_TOKEN`: optional for Feishu event verify
- `SENTIENTAGENT_V2_DEBUG`: set to `1` to print debug details to stderr, including:
  - request payload sent to the LLM runner
  - every function calling / tool calling trace with input and output
  - skill discovery and `read_skill` selection details
  - LLM event stream details (`text` / `function_call` / `function_response` / errors / finish_reason)

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

## Acknowledgements

This project is inspired by and partially adapted from [nanobot](https://github.com/HKUDS/nanobot).
Some implementation patterns and skill-related resources are derived from that project.
