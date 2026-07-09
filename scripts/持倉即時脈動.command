#!/usr/bin/env bash
# 持倉即時脈動 · 富邦持倉 + 現價 + RRG 收盤 vs 盤中 + exit playbook
# 建議：週一至五 09:10–13:20 手動點擊

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}"

echo "=============================================="
echo "  持倉即時脈動 · Fubon + RRG + Exit Playbook"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
echo ""

PYTHON="${ROOT}/.venv-fubon/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ 找不到 ${PYTHON}"
  echo "  富邦 Neo SDK 需 .venv-fubon（Python ≤3.13）"
  read -r -p "按 Enter 關閉…"
  exit 2
fi

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/order/holdings_pulse.py" \
  --sync-rrg-intraday \
  --write 2>&1)"
EXIT=$?
set -e

echo "${OUT}"
echo ""

STAMP="$(date '+%Y%m%d')"
if [[ "$EXIT" -eq 0 ]]; then
  echo "完成。報告：${ROOT}/reports/order/snapshots/holdings_pulse_${STAMP}.md"
else
  echo "有失敗步驟 (exit=${EXIT})。"
fi

# RRG 每檔一頁 PDF（盤中 WMA20/5/3 三腿 + 評語）· 需主環境 .venv（pandas/matplotlib）
VENV_MAIN="${ROOT}/.venv/bin/python"
if [[ -x "${VENV_MAIN}" ]]; then
  echo ""
  echo "── RRG 每檔一頁 PDF（WMA20/5/3）──"
  set +e
  "${VENV_MAIN}" "${ROOT}/scripts/order/render_holdings_rrg.py" --open 2>&1
  set -e
else
  echo "（略過 RRG PDF：找不到 ${VENV_MAIN}）"
fi
echo "完整輸出見上方表格。"
echo ""
read -r -p "按 Enter 關閉視窗…"
exit "${EXIT}"
