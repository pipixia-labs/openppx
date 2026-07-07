# openppx Operations Guide

## Install

```bash
cd openppx
pip install -e .
```

## Initialize

The install wizard module is currently not exposed for normal use. Use `doctor` plus manual configuration:

```bash
# Initialize or repair the minimal runnable configuration.
ppx doctor --fix

# Show the full diagnostic report.
ppx doctor
```

## Gateway Background Process

```bash
# Start the gateway in the background and write pid/meta/log files to ~/.openppx/log.
ppx gateway start --channels local,feishu

# Inspect status. Add --json for machine-readable output.
ppx gateway status
ppx gateway status --json

# Restart or stop.
ppx gateway restart --channels local,feishu
ppx gateway stop
```

Background runtime files:

- `~/.openppx/log/gateway.pid`
- `~/.openppx/log/gateway.meta.json`
- `~/.openppx/log/gateway.out.log`
- `~/.openppx/log/gateway.err.log`
- `~/.openppx/log/gateway.debug.log`

One-command smoke checks:

```bash
scripts/install_smoke.sh
scripts/install_smoke.sh --force
scripts/install_smoke.sh --with-gateway
```

Gateway service manifest management:

```bash
# Write a user-level service manifest without directly running launchctl or systemctl.
ppx gateway-service install
ppx gateway-service install --force --channels local,feishu

# Write, enable, and start immediately. This calls launchctl or systemctl --user.
ppx gateway-service install --enable

# Inspect manifest status for the current platform.
ppx gateway-service status
ppx gateway-service status --json
```

## Docker Sandbox

Dangerous commands can explicitly opt in to the Docker sandbox. Declarative Command/Python/Node skill APIs must not depend on recipe self-declaration for safety. Production or high-risk environments should use trusted runtime configuration:

```bash
export OPENPPX_SKILL_API_SANDBOX=docker
```

You can also configure it in `runtime.json`:

```json
{
  "env": {
    "OPENPPX_SKILL_API_SANDBOX": "docker",
    "OPENPPX_SANDBOX_IMAGE": "openppx-sandbox:dev"
  }
}
```

Default execution behavior stays unchanged unless this trusted configuration is set or the caller explicitly requests a sandbox.

Build the local sandbox image first:

```bash
ppx sandbox build-image --image openppx-sandbox:dev
```

Run real Docker integration tests:

```bash
OPENPPX_RUN_DOCKER_SANDBOX_TESTS=1 \
python -m pytest tests/test_docker_sandbox_integration.py -q
```

Diagnostics and cleanup:

```bash
ppx doctor
ppx sandbox prune
```

See [`SANDBOX.md`](./SANDBOX.md) for full configuration, controlled network/image enablement, and security boundaries.

### Common Missing Fields

- provider: `<provider>.apiKey`
  - Fill `providers.<provider>.apiKey`.
- Feishu: `channels.feishu.appId` / `channels.feishu.appSecret`
- Telegram: `channels.telegram.token`
- Discord: `channels.discord.token`
- DingTalk: `channels.dingtalk.clientId` / `channels.dingtalk.clientSecret`
- Slack: `channels.slack.botToken`
- WhatsApp: `channels.whatsapp.bridgeUrl`
- Email: `channels.email.consentGranted` / `channels.email.smtpHost` / `channels.email.smtpUsername` / `channels.email.smtpPassword`
- QQ: `channels.qq.appId` / `channels.qq.secret`

### Install and Repair Rule Sources

The core `doctor --fix` repair rules are intentionally table-driven:

- Channel environment backfill rules: `CHANNEL_ENV_BACKFILL_MAPPINGS` -> `DOCTOR_CHANNEL_ENV_BACKFILL_RULES`
- Provider doctor environment backfill: driven by `INSTALL_PROVIDER_SUMMARY_REQUIREMENTS`

Relevant code:

- `openppx/doctor_rules.py`: shared doctor/install rule tables and doctor backfill metadata
- `openppx/onboarding_adapters.py`: provider/channel onboarding adapter protocol, default adapter, and registry
- `openppx/cli.py`: command orchestration layer

When adding fields, prefer updating the rule table and tests instead of adding hard-coded branches in orchestration functions.

### Troubleshooting

- `Missing ... API key`
  - Open the target agent config at `~/.openppx/<agent_name>/config.json`, fill the enabled provider's `apiKey`, and run `ppx doctor` again.
  - If a local environment variable is already configured, run `ppx doctor --fix` to backfill the missing field.

- Missing `channels....` credential fields
  - Fill the matching field in the `channels` section of the target agent config and run `ppx doctor`.
  - Use `ppx doctor --json` if you are unsure which field is missing.

- `MCP server ... health check failed`
  - Confirm the MCP service process is reachable.
  - Use `ppx doctor --json` and inspect `mcp.health` details.

- All providers or channels are disabled
  - Run `ppx doctor --fix`. It enables the default provider and `channels.local` as the minimal runnable repair.

### `doctor --fix --json` Fields

Use this when feeding repair results into automation such as alerting, retries, or policy loops:

```bash
ppx doctor --fix --json
```

Key fields in the `fix` node:

- `fix.changes`: text list of changes applied in this run
- `fix.summary.counts`: counts by `defaults`, `env_backfill`, `legacy_migration`, and `other`
- `fix.reasonCodes`: counts grouped by standard reason code
- `fix.byRule`: per-rule aggregation with `applied`, `skipped`, `failed`, and `total`

Example:

```json
{
  "fix": {
    "applied": true,
    "dryRun": false,
    "changes": ["providers.google.apiKey <- GOOGLE_API_KEY"],
    "reasonCodes": {
      "provider.env.api_key_backfilled": 1,
      "channel.env.source_missing": 1
    },
    "byRule": {
      "provider_env_backfill": {"applied": 1, "skipped": 0, "failed": 0, "total": 1},
      "channel_env_backfill": {"applied": 0, "skipped": 1, "failed": 0, "total": 1}
    },
    "summary": {
      "counts": {"defaults": 0, "env_backfill": 1, "legacy_migration": 0, "other": 0}
    }
  }
}
```

## Runtime Modes

### Single-Turn Call

```bash
python -m openppx.cli -m "Describe what you can do"
```

Use explicit session identifiers when needed:

```bash
python -m openppx.cli -m "Describe what you can do" --user-id local --session-id demo001
```

Use ADK-native rewind when you need to roll back the current model-visible session context:

```bash
ppx rewind --user-id local --session-id demo001
ppx rewind --user-id local --session-id demo001 --before-invocation-id <invocation_id>
```

`rewind` makes later model context ignore rewound ADK events. It does not undo external side effects such as file writes, messages, command execution, or cron changes.

### ADK CLI Mode

```bash
adk run openppx
```

### Wrapper CLI

```bash
ppx run
```

### Common Tool Commands

```bash
ppx skills
ppx doctor
ppx doctor --fix
ppx doctor --fix-dry-run
ppx heartbeat status
ppx heartbeat status --json
ppx token stats
ppx token stats --provider google --limit 50
ppx token stats --json
ppx gateway-service install
ppx gateway-service status
ppx provider list
ppx provider status
ppx provider status --json
ppx provider login github-copilot
ppx provider login openai-codex
ppx provider login codex
ppx channels login
ppx channels bridge start
ppx channels bridge status
ppx channels bridge stop
```

## Gateway Modes

### Local Channel

```bash
python -m openppx.cli gateway run --channels local --interactive-local
```

### Multi-Channel Mode with Feishu

```bash
ppx gateway run --channels local,feishu --interactive-local
```

You can also set the default channels by environment variable:

```bash
export OPENPPX_CHANNELS=feishu
ppx gateway
```

## WhatsApp Bridge

`openppx` uses a local Node.js Bridge, Baileys plus WebSocket, for WhatsApp login and messaging.

```bash
# Foreground QR login.
ppx channels login

# Background bridge lifecycle.
ppx channels bridge start
ppx channels bridge status
ppx channels bridge stop
```

Quick checks:

```bash
scripts/whatsapp_bridge_e2e.sh full
scripts/whatsapp_bridge_e2e.sh smoke
```

## Cron Scheduling

`openppx` cron is an in-process scheduler and does not write system crontab entries. Jobs run only while the gateway is running.

- Storage file: `OPENPPX_WORKSPACE/.openppx/cron_jobs.json`
- Supported schedules: `every`, `cron` with optional `tz`, and `at`

Common commands:

```bash
ppx cron list
ppx cron add --name weather --message "check weather and summarize" --every 300
ppx cron add --name daily --message "daily report" --cron "0 9 * * 1-5" --tz Asia/Shanghai
ppx cron add --name reminder --message "remind me to review PR" --at 2026-02-19T09:30:00
ppx cron add --name push --message "send update" --every 600 --deliver --channel feishu --to ou_xxx
ppx cron run <job_id>
ppx cron enable <job_id>
ppx cron enable <job_id> --disable
ppx cron remove <job_id>
ppx cron status
```

## Token Statistics

`openppx` records token usage after each LLM call, including request/response, text/image, and timestamp information.

- Storage location: `~/.openppx/token_usage.db`, SQLite
- Granularity: one event per request/response
- Query commands:

```bash
ppx token stats
ppx token stats --provider google --limit 50
ppx token stats --provider openai --json
```

Notes:

- `token stats` prints summary statistics plus recent records by default.
- `--provider` filters by provider, such as `google` or `openai`.
- `--limit` controls the number of recent records, default `20`.
- `--json` emits machine-readable JSON for scripts or monitoring.
- Calls without provider usage information are not counted.

## Testing

```bash
source .venv/bin/activate
pytest -q
```

## Examples

```bash
python -m openppx.cli -m "search for the latest research progress today, and create a PPT for me."
python -m openppx.cli -m "download all PDF files from this page: https://bbs.kangaroo.study/forum.php?mod=viewthread&tid=467"
```
