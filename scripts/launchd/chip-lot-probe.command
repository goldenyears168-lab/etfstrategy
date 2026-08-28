#!/usr/bin/env bash
# 盤中單量結構探針 · 一次性（12:50 啟動、13:35 自動退出）· **唯讀，無任何送單路徑**。
# 獨立的第三條 Fubon 行情 websocket（訂閱 <=45×1 頻道，遠低於 108 撞牆教訓）。
# 目的與驗證計畫見 config/research.yaml topic chip-loud-accum-forward 的 notes。
#
# 安裝（Mac mini，一次性，不透過 scripts/install-launchd.sh 的下單層陣列）：
#   PLIST=~/Library/LaunchAgents/com.jackm4.goldenstocks.chip-lot-probe.plist
#   sed -e "s#{{CHIP_LOT_PROBE_LAUNCHER}}#$(pwd)/scripts/launchd/chip-lot-probe.command#g" \
#       -e "s#{{HOME}}#${HOME}#g" \
#       launchd/com.jackm4.goldenstocks.chip-lot-probe.plist.template > "${PLIST}"
#   launchctl bootstrap "gui/$(id -u)" "${PLIST}"
#
# 卸載：
#   launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.jackm4.goldenstocks.chip-lot-probe.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
APP_SUPPORT="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
STATE="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}"
WORKER_PY="${ROOT}/scripts/research/chip_intraday_lot_probe.py"

mkdir -p "${STATE}/logs/intraday" "${APP_SUPPORT}" "${HOME}/Library/Logs/com.jackm4.goldenstocks"
export TZ="${TZ:-Asia/Taipei}"
export PYTHONPATH="${ROOT}/src"

LOCKDIR="${APP_SUPPORT}/chip-lot-probe.lockdir"
if ! mkdir "${LOCKDIR}" 2>/dev/null; then
  holder="$(cat "${LOCKDIR}/pid" 2>/dev/null || echo "")"
  if [[ -n "${holder}" ]] && kill -0 "${holder}" 2>/dev/null; then
    echo "skip: already holding lock (pid=${holder}, alive)"; exit 0
  fi
  rm -rf "${LOCKDIR}"; mkdir "${LOCKDIR}" 2>/dev/null || { echo "skip: lock race lost"; exit 0; }
fi
echo "$$" > "${LOCKDIR}/pid"
trap 'rm -rf "${LOCKDIR}" 2>/dev/null || true' EXIT

_load_env_file() {
  [[ -r "$1" ]] || return 0
  set +e; set -a; source "$1" 2>/dev/null; local rc=$?; set +a; set -e
  [[ "${rc}" -ne 0 ]] && echo "WARN: cannot source $2 rc=${rc}"
  return 0
}
_load_env_file "${APP_SUPPORT}/order.env" "order.env"
_load_env_file "${STATE}/.env" "project .env"

if [[ "${RUN_CHIP_LOT_PROBE:-1}" == "0" ]]; then
  echo "chip-lot-probe skipped: RUN_CHIP_LOT_PROBE=0"; exit 0
fi

PYTHON="${ROOT}/.venv-fubon/bin/python"
if [[ ! -x "${PYTHON}" ]]; then echo "✗ missing .venv-fubon python: ${PYTHON}"; exit 1; fi
ROTATING_TEE="${ROOT}/scripts/launchd/rotating_tee.py"
LOG_PREFIX="${STATE}/logs/intraday/chip_lot_probe"

exec "${PYTHON}" "${WORKER_PY}" 2>&1 | "${PYTHON}" "${ROTATING_TEE}" "${LOG_PREFIX}"
