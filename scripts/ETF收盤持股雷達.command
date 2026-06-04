#!/usr/bin/env bash
# 方案 C · 排程②「收盤持股雷達」：官網持股 + changes + 部位意圖。
# 建議 Mac 排程：週一至五 16:30–18:00。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$ROOT"

echo "=============================================="
echo "  ② 收盤持股雷達（evening-holdings）"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
echo ""

export SYNC_PROFILE="evening-holdings"
"${ROOT}/scripts/daily_sync.sh" --holdings-only --quiet
EXIT=$?

echo ""
if [[ "$EXIT" -eq 0 ]]; then
  echo "收盤持股雷達完成。"
else
  echo "有失敗步驟 (exit=${EXIT})。"
fi
echo "log：${ROOT}/logs/daily_sync_$(date '+%Y%m%d').log"
echo ""
read -r -p "按 Enter 關閉視窗…"
exit "$EXIT"
