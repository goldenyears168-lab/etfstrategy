#!/usr/bin/env bash
# VIXTWN / VIX 日頻同步 · 每平日 20:30 · 唯讀行情 API + 寫入 market_vix_daily。
# 根因背景：sync_vixtwn.py 檔頭寫「可排程」但從未被排程，2026-08-16 後靠手動、
# 停更 20 天，直到 2026-09-03 留倉風險儀表上線才發現。本 job 根治之。
# 兩條線：sync_vixtwn.py（官方 finmind TaiwanOptionVix + Yahoo ^VIX，--days 7 自動補洞）
#        sync_vixtwn_computed.py（TXO 自算備援，近兩個月，與官方相關 0.989）
#
# 安裝（Mac mini，standalone，比照 chip-lot-probe 不進 install-launchd.sh LABELS）：
#   PLIST=~/Library/LaunchAgents/com.jackm4.goldenstocks.vixtwn-daily-sync.plist
#   sed -e "s#{{VIXTWN_DAILY_SYNC_LAUNCHER}}#$(pwd)/scripts/launchd/vixtwn-daily-sync.command#g" \
#       -e "s#{{HOME}}#${HOME}#g" \
#       launchd/com.jackm4.goldenstocks.vixtwn-daily-sync.plist.template > "${PLIST}"
#   launchctl bootstrap "gui/$(id -u)" "${PLIST}"

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
APP_SUPPORT="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
STATE="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}"
mkdir -p "${STATE}/logs" "${APP_SUPPORT}"
export TZ="${TZ:-Asia/Taipei}"
export PYTHONPATH="${ROOT}/src"

LOCKDIR="${APP_SUPPORT}/vixtwn-daily-sync.lockdir"
if ! mkdir "${LOCKDIR}" 2>/dev/null; then
  holder="$(cat "${LOCKDIR}/pid" 2>/dev/null || echo "")"
  if [[ -n "${holder}" ]] && kill -0 "${holder}" 2>/dev/null; then
    echo "skip: already running (pid=${holder})"; exit 0
  fi
  rm -rf "${LOCKDIR}"; mkdir "${LOCKDIR}" 2>/dev/null || { echo "skip: lock race"; exit 0; }
fi
echo "$$" > "${LOCKDIR}/pid"
trap 'rm -rf "${LOCKDIR}" 2>/dev/null || true' EXIT

_load_env_file() {
  [[ -r "$1" ]] || return 0
  set +e; set -a; source "$1" 2>/dev/null; local rc=$?; set +a; set -e
  [[ "${rc}" -ne 0 ]] && echo "WARN: cannot source $2 rc=${rc}"
  return 0
}
_load_env_file "${STATE}/.env" "project .env"

if [[ "${RUN_VIXTWN_DAILY_SYNC:-1}" == "0" ]]; then
  echo "vixtwn-daily-sync skipped: RUN_VIXTWN_DAILY_SYNC=0"; exit 0
fi

PYTHON="${ROOT}/.venv/bin/python"
LOG="${STATE}/logs/vixtwn_daily_sync_$(date +%Y%m%d).log"
{
  echo "[$(date -Iseconds)] start"
  "${PYTHON}" "${ROOT}/src/sync_vixtwn.py" --days 7 || echo "WARN: sync_vixtwn failed rc=$?"
  START="$(date -v-1m +%Y-%m)"; END="$(date +%Y-%m)"
  "${PYTHON}" "${ROOT}/src/sync_vixtwn_computed.py" --start "${START}" --end "${END}" \
    || echo "WARN: sync_vixtwn_computed failed rc=$?"
  echo "[$(date -Iseconds)] done"
} >> "${LOG}" 2>&1
