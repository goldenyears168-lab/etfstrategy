#!/usr/bin/env bash
# launchd 專用：00981A etfedge 持股回溯匯入 + 郵件通知
# 預設僅在 ETFEDGE_IMPORT_DATE（2026-06-17）執行；成功後自動卸載排程。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/launchd_etfedge-import.log"
LABEL="com.jackm4.etf.etfedge-import"
TARGET_DATE="${ETFEDGE_IMPORT_DATE:-2026-06-17}"
EXIT=1

exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd etfedge-import 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

TODAY="$(date '+%Y-%m-%d')"
if [[ "${TODAY}" != "${TARGET_DATE}" ]]; then
  echo "skip: today=${TODAY} target=${TARGET_DATE}"
  exit 0
fi

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
PYTHON="${ROOT}/.venv/bin/python"
DB="${ROOT}/data/stocks.db"

if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  export JOB_NOTIFY_EXTRA="venv 不存在"
else
  EXISTING=0
  if [[ -f "${DB}" ]]; then
    EXISTING="$(sqlite3 "${DB}" "SELECT COUNT(DISTINCT snapshot_date) FROM etf_holdings_meta WHERE etf_code='00981A' AND source='etfedge';" 2>/dev/null || echo 0)"
  fi
  if [[ "${EXISTING}" -ge 50 ]]; then
    echo "skip: etfedge already has ${EXISTING} snapshot dates"
    export JOB_NOTIFY_EXTRA="已匯入過（etfedge ${EXISTING} 日），略過"
    EXIT=0
  else
    set +e
    "${PYTHON}" "${ROOT}/scripts/import_etfedge_holdings.py"
    EXIT=$?
    set -e
    if [[ "${EXIT}" -eq 0 ]]; then
      AFTER="$(sqlite3 "${DB}" "SELECT COUNT(DISTINCT snapshot_date) FROM etf_holdings_meta WHERE etf_code='00981A' AND source='etfedge';")"
      export JOB_NOTIFY_EXTRA="etfedge snapshot 日數：${AFTER}"
    else
      export JOB_NOTIFY_EXTRA="import 結束碼 ${EXIT}，詳見 log"
    fi
  fi
fi

echo "=== launchd etfedge-import 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

"${ROOT}/scripts/etfedge_import_notify.sh" "${EXIT}" || true

if [[ "${EXIT}" -eq 0 ]]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  echo "✓ 已卸載一次性排程 ${LABEL}"
fi

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
