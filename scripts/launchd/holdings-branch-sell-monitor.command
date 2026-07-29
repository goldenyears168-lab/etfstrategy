#!/usr/bin/env bash
# launchd / 手動：持倉專家／大戶分點淨賣預警
# 週一至五 20:10 · email 預警 · 不下單

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
LAUNCHD_LOG="${ROOT}/logs/launchd_holdings-branch-sell-monitor.log"
RUN_LOG="${ROOT}/logs/holdings_branch_sell_${STAMP}.log"

mkdir -p "${ROOT}/logs"
exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd holdings-branch-sell 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src:${ROOT}/scripts/research"

if [[ -r "${ETF_DATA_DIR:-${ROOT}}/.env" ]]; then
  set +e
  set -a
  # shellcheck disable=SC1090
  source "${ETF_DATA_DIR:-${ROOT}}/.env" 2>/dev/null
  set +a
  set -e
fi

# --no-refresh 純讀 DB（18:30 branch-tape-prewarm 已補 POOLS∪持倉當日 tape）
export RUN_OPS_DIGEST_SYNC="${RUN_OPS_DIGEST_SYNC:-1}"
export RUN_ALERT_EMAIL=0
PYTHON="${ROOT}/.venv-fubon/bin/python"
EXIT=0
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv-fubon python: ${PYTHON}"
  EXIT=1
else
  set +e
  "${PYTHON}" "${ROOT}/scripts/order/run_holdings_branch_sell_monitor.py" \
    --no-refresh --write \
    2>&1 | tee "${RUN_LOG}"
  EXIT=${PIPESTATUS[0]}
  set -e
fi

echo "=== launchd holdings-branch-sell 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ "${EXIT}" -ne 0 ]]; then
  export JOB_NOTIFY_EXTRA="${JOB_NOTIFY_EXTRA:-持倉分點淨賣監控失敗，詳見 log}"
  /bin/bash "${ROOT}/scripts/job_notify.sh" \
    "持倉分點淨賣" "${EXIT}" "${RUN_LOG}" RUN_HOLDINGS_BRANCH_SELL_EMAIL || true
fi

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
