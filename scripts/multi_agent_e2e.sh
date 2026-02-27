#!/usr/bin/env bash
set -euo pipefail

# Multi-agent end-to-end smoke checks:
# - doctor --json
# - routes lint --json
# - routes stats --json (optional strict mode)
#
# Usage:
#   scripts/multi_agent_e2e.sh
#   scripts/multi_agent_e2e.sh --agents main,biz
#   scripts/multi_agent_e2e.sh --strict-routes-stats
#   scripts/multi_agent_e2e.sh --strict-warnings
#   scripts/multi_agent_e2e.sh --with-gateway-probe
#   scripts/multi_agent_e2e.sh --with-gateway-probe --strict-routes-stats --strict-warnings --agents main,biz

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

STRICT_ROUTES_STATS=0
STRICT_WARNINGS=0
WITH_GATEWAY_PROBE=0
AGENTS_CSV=""

while (($# > 0)); do
  case "$1" in
    --strict-routes-stats)
      STRICT_ROUTES_STATS=1
      shift
      ;;
    --strict-warnings)
      STRICT_WARNINGS=1
      shift
      ;;
    --with-gateway-probe)
      WITH_GATEWAY_PROBE=1
      shift
      ;;
    --agents)
      if (($# < 2)); then
        echo "--agents requires one comma-separated value, e.g. --agents main,biz" >&2
        exit 2
      fi
      AGENTS_CSV="$2"
      shift 2
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
warnings = payload.get("multiAgent", {}).get("warnings", [])
if warnings:
    print(f"[warn] doctor reports {len(warnings)} multi-agent warning(s)")
    for item in warnings[:10]:
        print("  -", item)
supported = payload.get("multiAgent", {}).get("scopeSupportedChannels", [])
if isinstance(supported, list) and supported:
    print("[info] doctor scopeSupportedChannels:", ",".join(str(item) for item in supported))
PY

if [[ -z "${AGENTS_CSV}" ]]; then
  AGENTS_CSV="$(python3 - "${TMP_DIR}/doctor.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
by_agent = payload.get("observability", {}).get("byAgent", {})
if isinstance(by_agent, dict) and by_agent:
    print(",".join(sorted(str(k) for k in by_agent.keys())))
else:
    print("main")
PY
)"
fi
echo "[multi-agent-e2e] agent targets: ${AGENTS_CSV}"

if [[ "${STRICT_WARNINGS}" == "1" ]]; then
  python3 - "${TMP_DIR}/doctor.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
warnings = payload.get("multiAgent", {}).get("warnings", [])
if warnings:
    print("[fail] doctor has multi-agent warnings under --strict-warnings")
    raise SystemExit(1)
PY
fi

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
warnings = payload.get("warnings", [])
if warnings:
    print(f"[warn] routes lint reports {len(warnings)} warning(s)")
    for item in warnings[:10]:
        print("  -", item)
supported = payload.get("scopeSupportedChannels", [])
if isinstance(supported, list) and supported:
    print("[info] routes lint scopeSupportedChannels:", ",".join(str(item) for item in supported))
PY

if [[ "${STRICT_WARNINGS}" == "1" ]]; then
  python3 - "${TMP_DIR}/routes_lint.json" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
warnings = payload.get("warnings", [])
if warnings:
    print("[fail] routes lint has warnings under --strict-warnings")
    raise SystemExit(1)
PY
fi

if [[ "${WITH_GATEWAY_PROBE}" == "1" ]]; then
  echo "[multi-agent-e2e] gateway probe (5s timeout)"
  if command -v timeout >/dev/null 2>&1; then
    timeout 5s openheron gateway --channels local >/dev/null 2>&1 || true
  else
    echo "[warn] timeout not found; skip gateway probe"
  fi
fi

for agent in ${AGENTS_CSV//,/ }; do
  echo "[multi-agent-e2e] heartbeat status --json --agent-id ${agent}"
  if openheron heartbeat status --json --agent-id "${agent}" >"${TMP_DIR}/heartbeat_status_${agent}.json"; then
    python3 - "${TMP_DIR}/heartbeat_status_${agent}.json" "${agent}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
agent = sys.argv[2]
if not isinstance(payload, dict):
    print(f"[fail] heartbeat status for agent={agent} is invalid JSON object")
    raise SystemExit(1)
print(
    "[ok] heartbeat status available for agent="
    f"{agent}, last_status={payload.get('last_status')}, target_mode={payload.get('target_mode')}"
)
PY
  else
    if [[ "${STRICT_ROUTES_STATS}" == "1" ]]; then
      echo "[fail] heartbeat status unavailable for agent=${agent} (strict mode enabled)." >&2
      exit 1
    fi
    echo "[warn] heartbeat status unavailable for agent=${agent}."
  fi

  echo "[multi-agent-e2e] routes stats --json --agent-id ${agent}"
  if openheron routes stats --json --agent-id "${agent}" >"${TMP_DIR}/routes_stats_${agent}.json"; then
    python3 - "${TMP_DIR}/routes_stats_${agent}.json" "${agent}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
agent = sys.argv[2]
if not payload.get("ok", False):
    print(f"[fail] routes stats returned ok=false for agent={agent}")
    raise SystemExit(1)
stats = payload.get("stats", {})
print(
    "[ok] routes stats available for agent="
    f"{agent}, totalMessagesInWindow={stats.get('totalMessagesInWindow', 0)}"
)
PY
  else
    if [[ "${STRICT_ROUTES_STATS}" == "1" ]]; then
      echo "[fail] routes stats unavailable for agent=${agent} (strict mode enabled)." >&2
      exit 1
    fi
    echo "[warn] routes stats unavailable for agent=${agent} (likely no gateway traffic yet)."
  fi
done

echo "[multi-agent-e2e] done"
