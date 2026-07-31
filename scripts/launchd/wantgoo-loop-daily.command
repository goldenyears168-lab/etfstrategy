#!/usr/bin/env bash
# launchd / 手動：玩股網每日情報迴圈
# 週一至五 20:15 · (1)抓白名單6位新文落庫 (2)記分板 score 昨日到期的作者 call
# (3)擷取: 有 ANTHROPIC_API_KEY 直接萃取, 否則匯出 bundle 供 Claude session/routine 處理
# 不下單 · 不寄信(失敗才寄) · 對外請求=玩股網公開 JSON (低頻研究用)
#
# 設計見 docs/wantgoo-intelligence-audit.md §10 + reports/research/wantgoo-daily-loop/。
# 擷取(把中文全文變結構化預測)需 LLM → 無金鑰時本 job 只做確定性的抓取+計分, 不會靜默失敗。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
RUN_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/wantgoo_loop_${STAMP}.log"
WL="${ROOT}/scripts/research/wantgoo_loop"

mkdir -p "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs"
echo ""
echo "=== launchd wantgoo-loop-daily 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"

if [[ -r "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/.env" ]]; then
  set +e; set -a
  # shellcheck disable=SC1090
  source "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/.env" 2>/dev/null
  set +a; set -e
fi

PYTHON="${ROOT}/.venv/bin/python"
EXIT=0
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"; EXIT=1
else
  set +e
  "${PYTHON}" "${WL}/fetch_blog_posts.py" --pages 1        2>&1 | tee -a "${RUN_LOG}"; E1=${PIPESTATUS[0]}
  "${PYTHON}" "${WL}/author_predictions.py" score          2>&1 | tee -a "${RUN_LOG}"; E2=${PIPESTATUS[0]}
  "${PYTHON}" "${WL}/extract_predictions.py"               2>&1 | tee -a "${RUN_LOG}"; E3=${PIPESTATUS[0]}
  "${PYTHON}" "${WL}/author_predictions.py" report         2>&1 | tee -a "${RUN_LOG}"
  set -e
  EXIT=$(( E1 | E2 | E3 ))
fi

echo "=== launchd wantgoo-loop-daily 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ "${EXIT}" -ne 0 && -x "${PYTHON}" ]]; then
  set +e
  "${PYTHON}" "${ROOT}/scripts/notify_job_result.py" \
    --subject-prefix="玩股網情報迴圈" --exit-code="${EXIT}" --log-path="${RUN_LOG}" \
    --extra="wantgoo-loop 執行失敗，詳見 log" --env-flag="RUN_WANTGOO_LOOP_EMAIL" 2>/dev/null
  set -e
fi

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi
exit "${EXIT}"
