#!/usr/bin/env bash
# launchd 專用：安聯台灣科技基金（ACDD04）月報公告偵測
# 週一至五 16:30 · 僅在新快照公布時寄信

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
LAUNCHD_LOG="${ROOT}/logs/launchd_mutual-fund-disclosure-watch.log"
RUN_LOG="${ROOT}/logs/mutual_fund_disclosure_watch_${STAMP}.log"

exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd mutual-fund-disclosure-watch 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
PYTHON="${ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  export JOB_NOTIFY_EXTRA="venv 不存在"
  EXIT=1
else
  set +e
  "${PYTHON}" "${ROOT}/scripts/run_mutual_fund_disclosure_watch.py" 2>&1 | tee "${RUN_LOG}"
  EXIT=${PIPESTATUS[0]}
  set -e
fi

echo "=== launchd mutual-fund-disclosure-watch 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ "${EXIT}" -ne 0 ]]; then
  export JOB_NOTIFY_EXTRA="${JOB_NOTIFY_EXTRA:-偵測失敗，詳見 log}"
  "${ROOT}/scripts/mutual_fund_disclosure_notify.sh" "${EXIT}" || true
fi

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
