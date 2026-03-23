#!/usr/bin/env bash
set -euo pipefail

# Lightweight end-to-end smoke for install + doctor (+ optional gateway probe).
# Usage:
#   scripts/install_smoke.sh
#   scripts/install_smoke.sh --force
#   scripts/install_smoke.sh --with-gateway

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

FORCE_FLAG=""
WITH_GATEWAY=0

while (($# > 0)); do
  case "$1" in
    --force)
      FORCE_FLAG="--force"
      shift
      ;;
    --with-gateway)
      WITH_GATEWAY=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if ! command -v openpipixia >/dev/null 2>&1; then
  echo "openpipixia command not found. Activate venv and run 'pip install -e .' first." >&2
  exit 1
fi

echo "[smoke] running install ${FORCE_FLAG}"
openpipixia install ${FORCE_FLAG}

echo "[smoke] running doctor"
openpipixia doctor

if [[ "${WITH_GATEWAY}" == "1" ]]; then
  echo "[smoke] running gateway probe"
  if command -v timeout >/dev/null 2>&1; then
    timeout 5s openpipixia gateway run --channels local || true
  else
    echo "[smoke] 'timeout' not found, skipping gateway probe"
  fi
fi

echo "[smoke] done"
