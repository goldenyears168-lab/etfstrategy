#!/usr/bin/env bash
# 方案 C · 排程②「收盤持股雷達」：官網持股 + Score + 終端 digest（詳表見 reports/）。
# 建議 Mac 排程：週一至五 16:30–18:00。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$ROOT"

echo "=============================================="
echo "  ② ETF 日報（持股 7 檔 + 成分變化）"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
echo ""

export SYNC_PROFILE="evening-holdings"
export RUN_STOCK_DAILY_LENS="${RUN_STOCK_DAILY_LENS:-1}"
export RUN_SUPABASE_LENS_SYNC="${RUN_SUPABASE_LENS_SYNC:-1}"
"${ROOT}/scripts/daily_sync.sh" --holdings-only --holdings-report
EXIT=$?

"${ROOT}/scripts/evening_holdings_notify.sh" "${EXIT}" || true

echo ""
if [[ "$EXIT" -eq 0 ]]; then
  echo "全部完成。"
else
  echo "有失敗步驟 (exit=${EXIT})。"
fi
echo "完整 log：${ROOT}/logs/daily_sync_$(date '+%Y%m%d').log"
echo ""
read -r -p "按 Enter 關閉視窗…"
exit "$EXIT"
