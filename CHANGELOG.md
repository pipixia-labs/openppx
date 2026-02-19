# Changelog

## 2026-02-19

### Added

- Added `channels` CLI commands for channel-side login helpers:
  - `sentientagent_v2 channels login [whatsapp]`
  - `sentientagent_v2 channels bridge start|status|stop [whatsapp]`
- Added packaged WhatsApp bridge sources under `sentientagent_v2/bridge/` so bridge bootstrap can work in installed environments.
- Added runtime bridge state persistence (`~/.sentientagent_v2/bridge/runtime_state.json`) for bridge lifecycle management.
- Added WhatsApp bridge readiness precheck for `doctor` and `gateway` when `whatsapp` channel is enabled.
- Added `scripts/whatsapp_bridge_e2e.sh` for QR login + bridge smoke flow + manual end-to-end validation guidance.

### Changed

- Updated README with WhatsApp QR bridge workflows, bridge lifecycle commands, and new related environment variables.
- Added `websockets>=12.0` dependency to project runtime requirements.

### Tested

- `./.venv/bin/pytest -q tests/test_cli.py`
- `./.venv/bin/pytest -q`
