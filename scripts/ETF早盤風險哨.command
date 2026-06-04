#!/usr/bin/env bash
# 方案 C · 排程①「早盤風險哨」：TEJ 日線 + 科技風險（TSM ADR）+ 可選 ETF 法人。
# 建議 Mac 排程：週一至五 08:25–08:40。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$ROOT"

echo "=============================================="
echo "  ① 早盤風險哨（morning-risk）"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
echo ""

export SYNC_PROFILE="morning-risk"
"${ROOT}/scripts/daily_sync.sh" --market-only --quiet
EXIT=$?

echo ""
if [[ "$EXIT" -eq 0 ]]; then
  echo "早盤風險哨完成。"
else
  echo "有失敗步驟 (exit=${EXIT})。"
fi
echo "log：${ROOT}/logs/daily_sync_$(date '+%Y%m%d').log（與收盤同日追加）"
echo ""
read -r -p "按 Enter 關閉視窗…"
exit "$EXIT"
