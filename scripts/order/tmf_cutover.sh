#!/bin/bash
# TMF 新架構一鍵切換：preflight import 檢查 → kickstart worker/sim-server → 等首輪對帳。
# KeepAlive 會在 kickstart 後 1 秒內重啟 worker；期望停機時間 < 5 秒（含一次 Fubon 登入）。
# 用法: scripts/order/tmf_cutover.sh [--dry]   # --dry 只做 preflight，不重啟
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PY="${PROJECT_ROOT}/.venv-fubon/bin/python"
UID_N="$(id -u)"
LOG_DIR="${GOLDENSTOCKS_DATA_DIR:-${HOME}/goldenstocks-data}/logs/intraday"
LOG_FILE="${LOG_DIR}/tmf_channel_live_$(date +%Y%m%d).log"

echo "[1/3] preflight: import check（.venv-fubon）"
PYTHONPATH="${PROJECT_ROOT}/src" "${PY}" -c "
import tmf_channel.worker_loop, tmf_channel.session_pool, tmf_channel.nq_gate
import tmf_channel.nq_1m_spread_gate
import tmf_channel.blotter, tmf_channel.engine, tmf_channel.harness
print('preflight ok')
"

if [[ "${1:-}" == "--dry" ]]; then
  echo "preflight-only（--dry）完成；未重啟任何行程"
  exit 0
fi

echo "[2/3] kickstart worker + sim-server"
launchctl kickstart -k "gui/${UID_N}/com.jackm4.goldenstocks.tmf-channel-poll"
launchctl kickstart -k "gui/${UID_N}/com.jackm4.goldenstocks.tmf-sim-server" || \
  echo "  (sim-server kickstart 失敗可忽略：非下單路徑)"

echo "[3/3] 等待新 worker 首輪（最多 90s）· log: ${LOG_FILE}"
deadline=$((SECONDS + 90))
seen_start=""
while (( SECONDS < deadline )); do
  line="$(tail -5 "${LOG_FILE}" 2>/dev/null | grep -E 'worker_start|"reason": "reconciled"|worker_exception' | tail -1 || true)"
  if [[ -z "${seen_start}" && "${line}" == *worker_start* ]]; then
    seen_start=1
    echo "  worker 已重啟: ${line}"
  elif [[ "${line}" == *worker_exception* ]]; then
    echo "✗ 新 worker 首輪例外，請立即檢查："
    tail -20 "${LOG_FILE}"
    exit 1
  elif [[ "${line}" == *'"reason": "reconciled"'* && -n "${seen_start}" ]]; then
    echo "✓ 切換完成，首輪對帳成功："
    echo "  ${line}"
    exit 0
  fi
  sleep 2
done
echo "⚠ 90s 內未見首輪對帳（若窗外 idle 屬正常）；手動確認："
echo "  tail -20 ${LOG_FILE}"
launchctl print "gui/${UID_N}/com.jackm4.goldenstocks.tmf-channel-poll" | grep -E 'state|pid' || true
