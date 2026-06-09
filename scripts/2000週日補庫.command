#!/usr/bin/env bash
# 方案 C · 排程③「週日深度補庫」：Beta + 基本面 + 成分股批次（後兩者 P0+ 後自動啟用）。
# 建議 Mac 排程：週日 20:00。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$ROOT"

echo "=============================================="
echo "  ③ 週日深度補庫（weekly-deep）"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
echo ""

export SYNC_PROFILE="weekly-deep"
"${ROOT}/scripts/weekly_sync.sh" --weekly-report
EXIT=$?

echo ""
if [[ "$EXIT" -eq 0 ]]; then
  echo "週日深度補庫完成。"
else
  echo "有失敗步驟 (exit=${EXIT})。"
fi
echo "log：${ROOT}/logs/weekly_sync_$(date '+%Y%m%d').log"
echo ""
read -r -p "按 Enter 關閉視窗…"
exit "$EXIT"
