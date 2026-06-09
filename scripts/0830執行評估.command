#!/usr/bin/env bash
# 方案 C · 排程①「執行評估」：TEJ 日線 + 科技風險 + E0 執行快照（pre_open）。
# 建議 Mac 排程：週一至五 08:25–08:40。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$ROOT"

echo "=============================================="
echo "  ① 執行評估（execution-eval）"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
echo ""

export SYNC_PROFILE="execution-eval"
"${ROOT}/scripts/daily_sync.sh" --market-only --execution-eval
EXIT=$?

echo ""
if [[ "$EXIT" -eq 0 ]]; then
  echo "執行評估完成。"
else
  echo "有失敗步驟 (exit=${EXIT})。"
fi
echo "log：${ROOT}/logs/daily_sync_$(date '+%Y%m%d').log（與收盤同日追加）"
echo ""
read -r -p "按 Enter 關閉視窗…"
exit "$EXIT"
