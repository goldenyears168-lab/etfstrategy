#!/usr/bin/env bash
# 相容入口：一次跑完 market + holdings（全量）。
# 日常營運請改用方案 C：ETF早盤風險哨.command + ETF收盤持股雷達.command（見 docs/PRD.md §5.2）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$ROOT"

echo "=============================================="
echo "  ETF 每日同步（全量 · 相容模式）"
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

"${ROOT}/scripts/daily_sync.sh" --quiet
EXIT=$?

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
