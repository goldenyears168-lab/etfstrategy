#!/usr/bin/env bash
# launchd 專用（無互動）：11:00 盤中持倉脈動

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday/launchd_intraday-midday-digest.log"
EXIT=1

mkdir -p "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday"
exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd intraday-midday-digest 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_INTRADAY_MIDDAY_DIGEST="${RUN_INTRADAY_MIDDAY_DIGEST:-1}"
export RUN_INTRADAY_MIDDAY_DIGEST_EMAIL="${RUN_INTRADAY_MIDDAY_DIGEST_EMAIL:-1}"
PYTHON="${ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  EXIT=1
else
  set +e
  "${PYTHON}" "${ROOT}/scripts/run_intraday_midday_digest.py" \
    --write --notify
  EXIT=$?
  set -e
fi

echo "=== launchd intraday-midday-digest 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
