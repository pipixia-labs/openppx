#!/usr/bin/env bash
set -euo pipefail

# Multi-agent end-to-end smoke checks:
# - doctor --json
# - routes lint --json
# - routes stats --json (optional strict mode)
#
# Usage:
#   scripts/multi_agent_e2e.sh
#   scripts/multi_agent_e2e.sh --strict-routes-stats
#   scripts/multi_agent_e2e.sh --with-gateway-probe
#   scripts/multi_agent_e2e.sh --with-gateway-probe --strict-routes-stats

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

STRICT_ROUTES_STATS=0
WITH_GATEWAY_PROBE=0

while (($# > 0)); do
  case "$1" in
    --strict-routes-stats)
      STRICT_ROUTES_STATS=1
      shift
      ;;
    --with-gateway-probe)
      WITH_GATEWAY_PROBE=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if ! command -v openheron >/dev/null 2>&1; then
  echo "openheron command not found. Activate venv and run 'pip install -e .' first." >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

echo "[multi-agent-e2e] doctor --json"
openheron doctor --json >"${TMP_DIR}/doctor.json"
python3 - "${TMP_DIR}/doctor.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
issues = payload.get("issues", [])
if issues:
    print("[fail] doctor issues:", issues)
    raise SystemExit(1)
print("[ok] doctor reports no blocking issues")
PY

echo "[multi-agent-e2e] routes lint --json"
openheron routes lint --json >"${TMP_DIR}/routes_lint.json"
python3 - "${TMP_DIR}/routes_lint.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not payload.get("ok", False):
    print("[fail] routes lint failed")
    print(payload.get("summary", {}))
    raise SystemExit(1)
print("[ok] routes lint passed")
PY

if [[ "${WITH_GATEWAY_PROBE}" == "1" ]]; then
  echo "[multi-agent-e2e] gateway probe (5s timeout)"
  if command -v timeout >/dev/null 2>&1; then
    timeout 5s openheron gateway --channels local >/dev/null 2>&1 || true
  else
    echo "[warn] timeout not found; skip gateway probe"
  fi
fi

echo "[multi-agent-e2e] routes stats --json"
if openheron routes stats --json >"${TMP_DIR}/routes_stats.json"; then
  python3 - "${TMP_DIR}/routes_stats.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not payload.get("ok", False):
    print("[fail] routes stats returned ok=false")
    raise SystemExit(1)
stats = payload.get("stats", {})
print("[ok] routes stats available, totalMessagesInWindow=", stats.get("totalMessagesInWindow", 0))
PY
else
  if [[ "${STRICT_ROUTES_STATS}" == "1" ]]; then
    echo "[fail] routes stats unavailable (strict mode enabled)." >&2
    exit 1
  fi
  echo "[warn] routes stats unavailable (likely no gateway traffic yet)."
fi

echo "[multi-agent-e2e] done"
