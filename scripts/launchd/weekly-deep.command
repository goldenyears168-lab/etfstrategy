#!/usr/bin/env bash
# launchd 專用（無互動）：③ 週日深度補庫
# 由 launchd 以 `open -gj` 觸發，避開 Documents 目錄 TCC 限制。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/launchd_weekly-deep.log"

exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd weekly-deep 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export SYNC_PROFILE="weekly-deep"
set +e
"${ROOT}/scripts/weekly_sync.sh" --weekly-report
EXIT=$?
set -e

echo "=== launchd weekly-deep 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
"${ROOT}/scripts/weekly_deep_notify.sh" "${EXIT}" || true

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
