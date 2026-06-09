#!/usr/bin/env bash
# E0 · 開盤前確認：產 order_intents →（可選）核准。
# 建議在 ① 執行評估之後；或單獨雙擊本檔。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
SRC="${ROOT}/src"
export PYTHONPATH="${SRC}${PYTHONPATH:+:${PYTHONPATH}}"

echo "=============================================="
echo "  E0 開盤確認（order intents）"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
echo ""

if [[ ! -x "$PYTHON" ]]; then
  echo "✗ 找不到 .venv"
  read -r -p "按 Enter 關閉…"
  exit 1
fi

"$PYTHON" "${SRC}/execution_eval.py" --mode pre_open --persist
echo ""
read -r -p "閱讀 reports/*_execution_eval.md 後，輸入 y 核准 approved [y/N]: " ans
if [[ "${ans,,}" == "y" ]]; then
  "$PYTHON" "${SRC}/execution_eval.py" --trade-date today --approve
  echo ""
  echo "核准完成。09:00 依開盤價於券商下單，或："
  echo "  order_intent_engine.py --apply-open --open-price 2330=1080,..."
else
  echo "未核准。"
fi
echo ""
read -r -p "按 Enter 關閉視窗…"
