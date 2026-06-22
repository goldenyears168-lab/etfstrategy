#!/usr/bin/env bash
# launchd 專用（無互動）：② 收盤持股雷達
# 由 launchd 以 `open -gj` 觸發，避開 Documents 目錄 TCC 限制。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/launchd_evening-holdings.log"

exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd evening-holdings 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export SYNC_PROFILE="evening-holdings"
# 收盤 cross-layer lens → Supabase（見 docs/修改計畫書.md）
export RUN_STOCK_DAILY_LENS="${RUN_STOCK_DAILY_LENS:-1}"
export RUN_SUPABASE_LENS_SYNC="${RUN_SUPABASE_LENS_SYNC:-1}"
set +e
"${ROOT}/scripts/daily_sync.sh" --holdings-only --holdings-report --quiet
EXIT=$?
set -e

echo "=== launchd evening-holdings 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
"${ROOT}/scripts/evening_holdings_notify.sh" "${EXIT}" || true

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
