#!/usr/bin/env bash
# ④ 盤中預覽（09:05+ · intraday）— 僅 preview，不寫 DB；預設 Yahoo 自動查價。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
SRC="${ROOT}/src"
export PYTHONPATH="${SRC}${PYTHONPATH:+:${PYTHONPATH}}"

echo "=============================================="
echo "  ④ 盤中預覽（intraday · 09:05+）"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
echo ""

if [[ ! -x "$PYTHON" ]]; then
  echo "✗ 找不到 .venv"
  read -r -p "按 Enter 關閉…"
  exit 1
fi

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

# 本腳本固定自動查價（不詢問終端輸入）
# 覆寫（可選）：INTRADAY_PREVIEW_PRICES=2330=2380,6223=5760
#              INTRADAY_PREVIEW_PRICE_SOURCE=auto|finmind|yahoo（預設 yahoo）
PRICE_SOURCE="${INTRADAY_PREVIEW_PRICE_SOURCE:-yahoo}"

set +e
if [[ -n "${INTRADAY_PREVIEW_PRICES:-}" ]]; then
  echo "報價  環境變數 INTRADAY_PREVIEW_PRICES"
  "$PYTHON" "${SRC}/execution_eval.py" \
    --mode intraday --trade-date today --preview \
    --prices "$INTRADAY_PREVIEW_PRICES"
elif [[ "$PRICE_SOURCE" == "yahoo" ]]; then
  echo "報價  Yahoo 1m（自動；延遲可能 15 分）"
  "$PYTHON" "${SRC}/execution_eval.py" \
    --mode intraday --trade-date today --preview \
    --price-source yahoo
else
  echo "報價  --price-source ${PRICE_SOURCE}"
  "$PYTHON" "${SRC}/execution_eval.py" \
    --mode intraday --trade-date today --preview \
    --price-source "$PRICE_SOURCE"
fi
EXIT=$?
set -e

echo ""
if [[ "$EXIT" -eq 0 ]]; then
  echo "✓ 盤中預覽完成（Yahoo 自動查價 · 未寫入 DB）"
  echo "  報告 reports/*_execution_eval_preview.md"
else
  echo "✗ 盤中預覽失敗 (exit=${EXIT})"
  echo "  檢查網路，或於 .env 設 INTRADAY_PREVIEW_PRICES=2330=2380,6223=5760"
fi
echo ""
read -r -p "按 Enter 關閉視窗…"
exit "$EXIT"
