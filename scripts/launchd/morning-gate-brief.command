#!/usr/bin/env bash
# launchd / 手動：台股 08:30 盤前定調簡報（開盤缺口預測 + regime）· 不下單
# 週一至五 08:25 觸發（趕在台指期 08:45 開盤前送達）
#   morning_gate_brief.py: yfinance 抓 NQ/ES 盤前 + DB VIX/VIXTWN → 缺口定調 markdown + (可選)寄信
# 寄信預設關：RUN_MORNING_BRIEF_EMAIL=1 才寄（沿用安全預設）。簡報檔一律寫出。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
RUN_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/morning_gate_brief_${STAMP}.log"
mkdir -p "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs"
echo "=== launchd morning-gate-brief 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

export PYTHONPATH="${ROOT}/src"
if [[ -r "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/.env" ]]; then
  set +e; set -a
  # shellcheck disable=SC1090
  source "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/.env" 2>/dev/null
  set +a; set -e
fi

PYTHON="${ROOT}/.venv/bin/python"
EMAIL_FLAG="--no-email"
[[ "${RUN_MORNING_BRIEF_EMAIL:-0}" == "1" ]] && EMAIL_FLAG=""

EXIT=0
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"; EXIT=1
else
  set +e
  # shellcheck disable=SC2086
  "${PYTHON}" "${ROOT}/src/morning_gate_brief.py" ${EMAIL_FLAG} 2>&1 | tee -a "${RUN_LOG}"
  EXIT=${PIPESTATUS[0]}
  set -e
fi

echo "=== launchd morning-gate-brief 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
if [[ "${EXIT}" -ne 0 && -x "${PYTHON}" ]]; then
  set +e
  "${PYTHON}" "${ROOT}/scripts/notify_job_result.py" \
    --subject-prefix="盤前定調" --exit-code="${EXIT}" --log-path="${RUN_LOG}" \
    --extra="morning_gate_brief 執行失敗，詳見 log" --env-flag="RUN_MORNING_BRIEF_EMAIL" 2>/dev/null
  set -e
fi
exit "${EXIT}"
