#!/usr/bin/env bash
# launchd 專用（無互動）：VCP Pivot Gate / Coil Close 盤中 brief（週一至五 13:00）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/launchd_vcp-funnel-specs.log"
EXIT=1

exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd vcp-funnel-specs 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
PYTHON="${ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  EXIT=1
else
  set +e
  "${PYTHON}" "${ROOT}/scripts/run_vcp_funnel_intraday.py"
  EXIT=$?
  set -e
fi

echo "=== launchd vcp-funnel-specs 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

"${ROOT}/scripts/launchd/supabase_slot_sync.sh" 1300

"${ROOT}/scripts/vcp_funnel_specs_notify.sh" "${EXIT}" || true

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
