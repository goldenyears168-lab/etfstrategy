#!/usr/bin/env bash
# 大戶盤中佈局監看 · 一次性（09:00 啟動、13:32 自動退出）· **唯讀，無任何送單路徑**。
# 09:00 起累積逐筆，12:00 / 13:00 / 13:30 各寄一封前十名，收盤後再寄一封。
# 判準與參數見 scripts/research/biglot_live_watch.py 檔頭；宇宙與門檻在
# ${GOLDENSTOCKS_DATA_DIR}/data/cache/pit_universe_tick/_live_calib.json（可離線重建）。
# 必須從 09:00 起算：全場 IC +0.151，只取 12:00-13:00 會掉到 +0.068 且對全場零增量。
# 獨立 Fubon 行情 websocket，訂閱 45×1 頻道（遠低於 108 撞牆教訓）。
#
# 安裝（Mac mini，一次性，不透過 scripts/install-launchd.sh 的下單層陣列）：
#   PLIST=~/Library/LaunchAgents/com.jackm4.goldenstocks.biglot-live-watch.plist
#   sed -e "s#{{BIGLOT_LIVE_WATCH_LAUNCHER}}#$(pwd)/scripts/launchd/biglot-live-watch.command#g" \
#       -e "s#{{HOME}}#${HOME}#g" \
#       launchd/com.jackm4.goldenstocks.biglot-live-watch.plist.template > "${PLIST}"
#   launchctl bootstrap "gui/$(id -u)" "${PLIST}"
#
# 卸載：
#   launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.jackm4.goldenstocks.biglot-live-watch.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
APP_SUPPORT="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
STATE="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}"
WORKER_PY="${ROOT}/scripts/research/biglot_live_watch.py"

mkdir -p "${STATE}/logs/intraday" "${APP_SUPPORT}" "${HOME}/Library/Logs/com.jackm4.goldenstocks"
export TZ="${TZ:-Asia/Taipei}"
export PYTHONPATH="${ROOT}/src"

LOCKDIR="${APP_SUPPORT}/biglot-live-watch.lockdir"
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

if [[ "${RUN_BIGLOT_LIVE_WATCH:-1}" == "0" ]]; then
  echo "biglot-live-watch skipped: RUN_BIGLOT_LIVE_WATCH=0"; exit 0
fi

PYTHON="${ROOT}/.venv-fubon/bin/python"
if [[ ! -x "${PYTHON}" ]]; then echo "✗ missing .venv-fubon python: ${PYTHON}"; exit 1; fi
ROTATING_TEE="${ROOT}/scripts/launchd/rotating_tee.py"
LOG_PREFIX="${STATE}/logs/intraday/biglot_live_watch"

exec "${PYTHON}" "${WORKER_PY}" 2>&1 | "${PYTHON}" "${ROTATING_TEE}" "${LOG_PREFIX}"
