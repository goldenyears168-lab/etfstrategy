#!/bin/bash
# dayflip-short 新架構（StartInterval 冷啟動 → KeepAlive 常駐 worker）一鍵切換：
# preflight import 檢查 → kickstart worker → 等首輪 tick。
# 用法: scripts/order/dayflip_short_cutover.sh [--dry]   # --dry 只做 preflight，不重啟
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PY="${PROJECT_ROOT}/.venv/bin/python"
UID_N="$(id -u)"
LOG_DIR="${GOLDENSTOCKS_DATA_DIR:-${HOME}/goldenstocks-data}/logs/intraday"
LOG_FILE="${LOG_DIR}/dayflip_short_$(date +%Y%m%d).log"

echo "[1/3] preflight: import check（.venv）"
PYTHONPATH="${PROJECT_ROOT}/src:${PROJECT_ROOT}/scripts/research" "${PY}" -c "
import order.dayflip_short_worker_loop, order.dayflip_short_session_pool, order.dayflip_short_order
print('preflight ok')
"

if [[ "${1:-}" == "--dry" ]]; then
  echo "preflight-only（--dry）完成；未重啟任何行程"
  exit 0
fi

echo "[2/3] kickstart worker"
launchctl kickstart -k "gui/${UID_N}/com.jackm4.goldenstocks.dayflip-short-poll"

echo "[3/3] 等待新 worker 首輪 tick（最多 90s）· log: ${LOG_FILE}"
deadline=$((SECONDS + 90))
seen_start=""
while (( SECONDS < deadline )); do
  line="$(tail -5 "${LOG_FILE}" 2>/dev/null | grep -E 'worker_start|"action":|worker_exception' | tail -1 || true)"
  if [[ -z "${seen_start}" && "${line}" == *worker_start* ]]; then
    seen_start=1
    echo "  worker 已重啟: ${line}"
  elif [[ "${line}" == *worker_exception* ]]; then
    echo "✗ 新 worker 首輪例外，請立即檢查："
    tail -20 "${LOG_FILE}"
    exit 1
  elif [[ "${line}" == *'"action":'* && -n "${seen_start}" ]]; then
    echo "✓ 切換完成，首輪 tick 成功："
    echo "  ${line}"
    exit 0
  fi
  sleep 2
done
echo "⚠ 90s 內未見首輪 tick（若窗外 idle 屬正常，例如非交易日/收盤後）；手動確認："
echo "  tail -20 ${LOG_FILE}"
launchctl print "gui/${UID_N}/com.jackm4.goldenstocks.dayflip-short-poll" | grep -E 'state|pid' || true
