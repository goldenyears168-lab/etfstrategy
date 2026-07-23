#!/usr/bin/env bash
# 持倉即時脈動 · 富邦持倉狀態 + 現價 + RRG 收盤 vs 盤中
# 建議：週一至五 09:10–13:20 手動點擊

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}"

echo "=============================================="
echo "  持倉即時脈動 · Fubon + RRG"
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

# 串流輸出（勿整段 capture）：盤中 sync 約 1–2 分，卡住時才看得到進度／錯誤
set +e
"${PYTHON}" "${ROOT}/scripts/order/holdings_pulse.py" \
  --sync-rrg-intraday \
  --write
EXIT=$?
set -e

echo ""

STAMP="$(date '+%Y%m%d')"
PULSE_MD="${ROOT}/reports/order/snapshots/holdings_pulse_${STAMP}.md"
PULSE_JSON="${ROOT}/reports/order/snapshots/holdings_pulse_${STAMP}.json"
if [[ "$EXIT" -eq 0 ]]; then
  echo "完成。報告：${PULSE_MD}"
else
  echo "有失敗步驟 (exit=${EXIT})。"
fi

# RRG HTML（盤中 WMA20/5/3 三腿 + 評語 + 持倉狀態）· 需主環境 .venv（pandas/matplotlib）
# 必須餵入本輪 pulse JSON，且依賴 Yahoo ^TWII（IX0001/TAIEX）5m 才能走盤中口徑。
VENV_MAIN="${ROOT}/.venv/bin/python"
if [[ -x "${VENV_MAIN}" ]]; then
  echo ""
  echo "── RRG HTML（WMA20/5/3）──"
  RRG_ARGS=(--caliber auto --open)
  if [[ -f "${PULSE_JSON}" ]]; then
    RRG_ARGS+=(--json "${PULSE_JSON}")
  else
    echo "⚠ 找不到 ${PULSE_JSON}，改用最新 holdings_pulse_*.json"
  fi
  set +e
  RRG_LOG="$(mktemp "${TMPDIR:-/tmp}/holdings_rrg.XXXXXX")"
  "${VENV_MAIN}" "${ROOT}/scripts/order/render_holdings_rrg.py" "${RRG_ARGS[@]}" 2>&1 | tee "${RRG_LOG}"
  RRG_EXIT=${PIPESTATUS[0]}
  set -e
  if [[ "$RRG_EXIT" -ne 0 ]]; then
    echo "⚠ RRG HTML 失敗 (exit=${RRG_EXIT})"
    EXIT=$(( EXIT > RRG_EXIT ? EXIT : RRG_EXIT ))
  elif grep -q '收盤口徑' "${RRG_LOG}"; then
    echo "⚠ RRG 仍為收盤口徑：多半缺 IX0001/TAIEX（^TWII）盤中 K；請確認網路後重跑。"
  fi
  rm -f "${RRG_LOG}"
else
  echo "（略過 RRG HTML：找不到 ${VENV_MAIN}）"
fi

# 持倉分點淨賣：專家池（有則列）＋跨池面板 K/N≥5000萬（每檔）
# 與 mini 20:10 holdings-branch-sell-monitor 同腳本；此處 dry-run 不寄信
echo ""
echo "── 持倉分點淨賣（專家＋面板 K/N）──"
set +e
"${PYTHON}" "${ROOT}/scripts/order/run_holdings_branch_sell_monitor.py" \
  --no-refresh \
  --write \
  --dry-run
SELL_EXIT=$?
set -e
if [[ "$SELL_EXIT" -ne 0 ]]; then
  echo "⚠ 分點淨賣監控失敗 (exit=${SELL_EXIT})"
  EXIT=$(( EXIT > SELL_EXIT ? EXIT : SELL_EXIT ))
else
  SELL_STAMP="$(date '+%Y%m%d')"
  SELL_MD="${ROOT}/reports/order/snapshots/holdings_branch_sell/branch_sell_${SELL_STAMP}.md"
  SELL_HTML="${ROOT}/reports/order/snapshots/holdings_branch_sell/branch_sell_${SELL_STAMP}.html"
  echo "分點報告：${SELL_MD}"
  [[ -f "${SELL_HTML}" ]] && echo "分點 HTML：${SELL_HTML}"
fi

echo "完整輸出見上方【持倉狀態】、分點淨賣與瀏覽器 RRG HTML。"
echo ""
read -r -p "按 Enter 關閉視窗…"
exit "${EXIT}"
