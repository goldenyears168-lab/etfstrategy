#!/usr/bin/env bash
# launchd：週一至五 09:00–09:04 每分鐘一輪追價（限價追賣一 · 最多 5 輪）
# 僅處理 user_def=chase_open；不撤人工掛單

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/launchd_order-chase-open.log"
EXIT=1

exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd order-chase-open 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
PYTHON="${ROOT}/.venv-fubon/bin/python"
CHASE="${ROOT}/scripts/order/chase_scheduled.py"
SPEC="${ROOT}/reports/order/intents/scheduled/open_market_10000.json"

if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing .venv-fubon python: ${PYTHON}"
  EXIT=1
else
  set +e
  "${PYTHON}" "${CHASE}" "${SPEC}"
  EXIT=$?
  set -e
fi

echo "=== launchd order-chase-open 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
