#!/usr/bin/env bash
# 方案 C · 排程④「策略回顧」：訊號事後歸因 + Paper 10 萬每日全換（只讀 DB）。
# 隨時可跑；建議每週至少一次。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
SRC="${ROOT}/src"
export PYTHONPATH="${SRC}${PYTHONPATH:+:${PYTHONPATH}}"

echo "=============================================="
echo "  ④ 策略回顧（signal-review）"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
echo ""

if [[ ! -x "$PYTHON" ]]; then
  echo "✗ 找不到 .venv，請先在專案目錄建立虛擬環境"
  read -r -p "按 Enter 關閉…"
  exit 1
fi

"$PYTHON" "${SRC}/signal_review.py" --lookback-trading-days 7 --lookback-event-days 20
EXIT=$?

echo ""
if [[ "$EXIT" -eq 0 ]]; then
  echo "策略回顧完成。"
else
  echo "執行失敗 (exit=${EXIT})。"
fi
echo "log：${ROOT}/logs/signal_review_$(date '+%Y%m%d').log"
echo ""
read -r -p "按 Enter 關閉視窗…"
exit "$EXIT"
