#!/usr/bin/env bash
set -euo pipefail

# End-to-end helper for WhatsApp QR bridge workflows.
# Usage:
#   scripts/whatsapp_bridge_e2e.sh login
#   scripts/whatsapp_bridge_e2e.sh smoke
#   scripts/whatsapp_bridge_e2e.sh full
#
# Environment:
#   PYTHON_BIN                     Python executable (default: ./.venv/bin/python)
#   SENTIENTAGENT_V2_CHANNELS      Defaults to "whatsapp" for this script
#   WHATSAPP_BRIDGE_URL            Defaults to ws://127.0.0.1:3001 if unset
#   WHATSAPP_BRIDGE_TOKEN          Optional token; should match bridge config
#   RUN_DOCTOR                     1/0, whether smoke/full runs doctor --json (default: 1)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "[error] Python executable not found: ${PYTHON_BIN}" >&2
  echo "Set PYTHON_BIN or create venv at ${REPO_ROOT}/.venv" >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "[error] npm is required (Node.js >= 20)." >&2
  exit 1
fi

export SENTIENTAGENT_V2_CHANNELS="${SENTIENTAGENT_V2_CHANNELS:-whatsapp}"
export WHATSAPP_BRIDGE_URL="${WHATSAPP_BRIDGE_URL:-ws://127.0.0.1:3001}"

run_cli() {
  "${PYTHON_BIN}" -m sentientagent_v2.cli "$@"
}

cmd_login() {
  echo "[step] Starting foreground QR login helper..."
  echo "[note] Scan QR in this terminal with WhatsApp Linked Devices."
  run_cli channels login
}

cmd_smoke() {
  echo "[step] Starting bridge in background..."
  run_cli channels bridge start

  echo "[step] Checking bridge status..."
  run_cli channels bridge status

  if [[ "${RUN_DOCTOR:-1}" == "1" ]]; then
    echo "[step] Running doctor --json (may fail if provider credentials are missing)..."
    set +e
    run_cli doctor --json
    doctor_code=$?
    set -e
    echo "[info] doctor exit code: ${doctor_code}"
  fi

  echo "[step] Stopping bridge..."
  run_cli channels bridge stop

  echo "[done] Bridge smoke flow completed."
}

cmd_full() {
  echo "[flow] 1) QR login (foreground)"
  cmd_login

  echo "[flow] 2) Background bridge smoke"
  cmd_smoke

  cat <<'EOF'
[next] Manual runtime validation:
  1) Start bridge: ./.venv/bin/python -m sentientagent_v2.cli channels bridge start
  2) Start gateway: ./.venv/bin/python -m sentientagent_v2.cli gateway --channels whatsapp
  3) Send a WhatsApp message to your linked account/group and observe gateway logs.
  4) Stop gateway (Ctrl+C), then stop bridge: ./.venv/bin/python -m sentientagent_v2.cli channels bridge stop
EOF
}

usage() {
  cat <<'EOF'
Usage:
  scripts/whatsapp_bridge_e2e.sh login
  scripts/whatsapp_bridge_e2e.sh smoke
  scripts/whatsapp_bridge_e2e.sh full
EOF
}

main() {
  local cmd="${1:-}"
  case "${cmd}" in
    login)
      cmd_login
      ;;
    smoke)
      cmd_smoke
      ;;
    full)
      cmd_full
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
