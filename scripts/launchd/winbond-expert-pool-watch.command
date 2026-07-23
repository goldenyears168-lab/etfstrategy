#!/usr/bin/env bash
# launchd / 手動：夜間研究觀測合併 digest（專家池 + 松山）
# 週一至五 20:00 · 一封信（含今日皆無訊號）· 不下單

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
LAUNCHD_LOG="${ROOT}/logs/launchd_winbond-expert-pool-watch.log"
RUN_LOG="${ROOT}/logs/evening_research_watch_${STAMP}.log"

mkdir -p "${ROOT}/logs"
exec >>"${LAUNCHD_LOG}" 2>&1
echo ""
echo "=== launchd evening-watch digest 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src:${ROOT}/scripts/research"
PYTHON="${ROOT}/.venv/bin/python"

if [[ -r "${ROOT}/.env" ]]; then
  set +e
  set -a
  # shellcheck disable=SC1090
  source "${ROOT}/.env" 2>/dev/null
  set +a
  set -e
fi
export RUN_OPS_DIGEST_SYNC="${RUN_OPS_DIGEST_SYNC:-1}"

EXIT=0
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  EXIT=1
else
  set +e
  # --refresh-days 0：關掉 POOLS 分點補檔（18:30 branch-tape-prewarm 已補）；
  # 松山／新店分點掃描是另一套機制，刻意不加 --no-refresh 以免一起關掉
  "${PYTHON}" "${ROOT}/scripts/research/run_evening_research_watch_digest.py" \
    --refresh-days 0 \
    2>&1 | tee "${RUN_LOG}"
  EXIT=${PIPESTATUS[0]}
  set -e
fi

echo "=== launchd evening-watch digest 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ "${EXIT}" -ne 0 ]]; then
  export JOB_NOTIFY_EXTRA="${JOB_NOTIFY_EXTRA:-夜間觀測 digest 失敗，詳見 log}"
  /bin/bash "${ROOT}/scripts/job_notify.sh" \
    "夜間觀測" "${EXIT}" "${RUN_LOG}" RUN_EXPERT_POOL_EMAIL || true
fi

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi

exit "${EXIT}"
