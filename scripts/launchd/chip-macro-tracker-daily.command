#!/usr/bin/env bash
# launchd / 手動：籌碼→大盤 風控燈號 daily tracker
# 週一至五 18:00 · refresh panel(FinMind) + 6 盞燈 + 資料新鮮度 gate + 隔日驗證 backfill
# + ops-console 推送 · 不下單 · 不寄信(失敗才寄)
#
# 隔日驗證 = daily_tracker.backfill_outcomes(): 事後 join TAIEX fwd_1d/fwd_5d + call_correct(無前視)。
# 資料新鮮度 = compute_staleness(): panel 落後 >=2 交易日 → 報告頂端紅字 + history 標 stale。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
RUN_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/chip_macro_tracker_${STAMP}.log"

mkdir -p "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs"
echo ""
echo "=== launchd chip-macro-tracker-daily 開始 $(date '+%Y-%m-%d %H:%M:%S') ==="

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
  # --no-ops：2026-08-17 起停用 Supabase（使用者不再使用、要停 Pro Plan $25/月 扣款）。
  # 本地 panel/6燈/backfill_outcomes 全部照常，只是不再把 chip_macro payload 推上
  # 公開 ops-console。要恢復就拿掉這個 flag（同時要把 .env 的 RUN_SUPABASE_* 打開）。
  "${PYTHON}" "${ROOT}/scripts/research/chip_macro/daily_tracker.py" \
    --no-ops \
    2>&1 | tee -a "${RUN_LOG}"
  EXIT=${PIPESTATUS[0]}
  set -e
fi

echo "=== launchd chip-macro-tracker-daily 結束 exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ "${EXIT}" -ne 0 && -x "${PYTHON}" ]]; then
  set +e
  "${PYTHON}" "${ROOT}/scripts/notify_job_result.py" \
    --subject-prefix="籌碼大盤tracker" --exit-code="${EXIT}" --log-path="${RUN_LOG}" \
    --extra="daily_tracker 執行失敗，詳見 log" --env-flag="RUN_CHIP_MACRO_TRACKER_EMAIL" 2>/dev/null
  set -e
fi

if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
  /usr/bin/osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 || true
fi
exit "${EXIT}"
