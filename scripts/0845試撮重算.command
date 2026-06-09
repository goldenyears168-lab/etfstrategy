#!/usr/bin/env bash
# ② 試撮重算（08:45–08:59 · auction）— 不重跑 daily_sync，僅重算執行快照。
# 建議在 ① 0830執行評估.command 之後；定稿後再 --approve。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
SRC="${ROOT}/src"
export PYTHONPATH="${SRC}${PYTHONPATH:+:${PYTHONPATH}}"

echo "=============================================="
echo "  ② 試撮重算（auction · 08:45–08:59）"
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
  echo "已載入 .env（FinMind=$([ -n "${FINMIND_TOKEN:-}" ] && echo set || echo missing) price_source=${EXECUTION_EVAL_PRICE_SOURCE:-manual}）"
  echo ""
fi

PRICE_SOURCE="${EXECUTION_EVAL_PRICE_SOURCE:-manual}"
PRICE_EXAMPLE="2330=2310,6223=5775,1303=104"

run_auction() {
  "$PYTHON" "${SRC}/execution_eval.py" \
    --mode auction \
    --trade-date today \
    --persist \
    "$@"
}

prompt_manual_or_yahoo() {
  echo "請輸入試撮價，或直接 Enter 從 Yahoo 查（1 分 K；延遲可能 15 分）"
  echo "  例 ${PRICE_EXAMPLE}"
  read -r -p "試撮 --prices [Enter=Yahoo]: " MANUAL_PRICES
  if [[ -z "${MANUAL_PRICES// }" ]]; then
    echo "報價  --price-source yahoo"
    run_auction --price-source yahoo
  else
    run_auction --prices "$MANUAL_PRICES"
  fi
}

EXIT=2
if [[ "$PRICE_SOURCE" == "yahoo" ]]; then
  echo "報價  --price-source yahoo（Yahoo 1m；延遲可能 15 分）"
  set +e
  run_auction --price-source yahoo
  EXIT=$?
  set -e
elif [[ "$PRICE_SOURCE" == "auto" || "$PRICE_SOURCE" == "finmind" ]]; then
  if [[ -z "${FINMIND_TOKEN:-}" ]]; then
    echo "⚠ 未設 FINMIND_TOKEN，改 Yahoo 或手動輸入"
    set +e
    prompt_manual_or_yahoo
    EXIT=$?
    set -e
  else
    echo "報價  --price-source ${PRICE_SOURCE}（FinMind tick；失敗則 Yahoo 備援或手動）"
    set +e
    run_auction --price-source "$PRICE_SOURCE"
    EXIT=$?
    set -e
    if [[ "$EXIT" -ne 0 ]]; then
      echo ""
      echo "⚠ 自動報價失敗（register 帳號無 tick；可 Enter 改 Yahoo 或貼券商試撮價）"
      set +e
      prompt_manual_or_yahoo
      EXIT=$?
      set -e
    fi
  fi
else
  set +e
  prompt_manual_or_yahoo
  EXIT=$?
  set -e
fi

echo ""
if [[ "$EXIT" -eq 0 ]]; then
  echo "試撮重算完成（已寫入 order_intents · reports/*_execution_eval_auction.md）。"
  echo "下一步  核准：.venv/bin/python src/execution_eval.py --trade-date today --approve"
  echo "        或雙擊 0850開盤確認.command"
else
  echo "試撮重算失敗 (exit=${EXIT})。可改用手動價，例如："
  echo "  PYTHONPATH=src .venv/bin/python src/execution_eval.py --mode auction --persist --prices ${PRICE_EXAMPLE}"
fi
echo ""
read -r -p "按 Enter 關閉視窗…"
exit "$EXIT"
