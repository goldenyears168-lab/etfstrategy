#!/usr/bin/env bash
# launchd / 手動：分點 tape 補檔（POOLS ∪ 富邦持倉）
# 週一至五 18:30 · 不下單

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
LAUNCHD_LOG="${ROOT}/logs/launchd_branch-tape-prewarm.log"
RUN_LOG="${ROOT}/logs/branch_tape_prewarm_${STAMP}.log"

mkdir -p "${ROOT}/logs"
exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd branch-tape-prewarm 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src:${ROOT}/scripts/research:${ROOT}/scripts/order"

if [[ -r "${ETF_DATA_DIR:-${ROOT}}/.env" ]]; then
  set +e
  set -a
  # shellcheck disable=SC1090
  source "${ETF_DATA_DIR:-${ROOT}}/.env" 2>/dev/null
  set +a
  set -e
fi

PYTHON="${ROOT}/.venv-fubon/bin/python"
EXIT=0
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv-fubon python: ${PYTHON}"
  EXIT=1
else
  set +e
  "${PYTHON}" "${ROOT}/scripts/order/run_branch_tape_prewarm.py" \
    --refresh-days 2 \
    2>&1 | tee "${RUN_LOG}"
  EXIT=${PIPESTATUS[0]}
  set -e
fi

echo "=== launchd branch-tape-prewarm 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ "${EXIT}" -ne 0 ]]; then
  export JOB_NOTIFY_EXTRA="${JOB_NOTIFY_EXTRA:-分點 tape 補檔失敗，詳見 log}"
  /bin/bash "${ROOT}/scripts/job_notify.sh" \
    "分點補檔" "${EXIT}" "${RUN_LOG}" RUN_BRANCH_TAPE_PREWARM_EMAIL || true
fi

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
