#!/usr/bin/env bash
# 雙擊執行：ETF 每日同步（5 檔，4 項）
# 可將此檔案複製或建立替身到桌面，方便每天手動執行。

set -euo pipefail

ROOT="/Users/jackm4/Documents/ETF/股票研究"
cd "$ROOT"

echo "=============================================="
echo "  ETF 每日同步（5 檔）"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
echo ""

if [[ ! -x "${ROOT}/scripts/daily_sync.sh" ]]; then
  echo "✗ 找不到 scripts/daily_sync.sh"
  echo "  路徑：${ROOT}/scripts/daily_sync.sh"
  read -r -p "按 Enter 關閉…"
  exit 1
fi

if [[ ! -x "${ROOT}/.venv/bin/python" ]]; then
  echo "✗ 找不到 .venv，請先在專案目錄建立虛擬環境"
  read -r -p "按 Enter 關閉…"
  exit 1
fi

"${ROOT}/scripts/daily_sync.sh"
EXIT=$?

echo ""
echo "----------------------------------------------"
echo "  重複按：安全。日線/法人會覆寫同日；持股未更新會 Skip"
echo "  要新的持股 changes：需不同交易日各跑一次"
echo "----------------------------------------------"
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
