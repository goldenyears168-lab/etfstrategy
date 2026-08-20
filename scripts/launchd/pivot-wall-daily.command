#!/usr/bin/env bash
# launchd：每日 06:00 觸發一次，處理「前一個日曆日」的日盤與夜盤。
# 研究層資料累積（scripts/research/），**唯讀，不下單、不改倉、不寄信**。
# 手動除錯：open -gj scripts/launchd/pivot-wall-daily.command
#
# 在累積什麼：主要轉折（ZigZag 60 點）處防守側是否有牆（≥3× 前 10 分鐘基準），
# 對照組是離任何轉折 ≥300 秒的隨機時刻。2026-08-20 單一夜盤量到 MXF 47.5% vs
# 14.5%（倍率 3.27×），但那是 n=1，而且反向檢定一加波動分層就塌。判準是
# **跨 session-day 的一致性**，不是任何單日的 p 值。
#
# 安裝（Mac mini，一次性，不透過 scripts/install-launchd.sh 的下單層陣列）：
#   PLIST=~/Library/LaunchAgents/com.jackm4.goldenstocks.pivot-wall-daily.plist
#   sed -e "s#{{PIVOT_WALL_DAILY_LAUNCHER}}#$(pwd)/scripts/launchd/pivot-wall-daily.command#g" \
#       -e "s#{{HOME}}#${HOME}#g" \
#       launchd/com.jackm4.goldenstocks.pivot-wall-daily.plist.template > "${PLIST}"
#   launchctl bootstrap "gui/$(id -u)" "${PLIST}"
#
# 卸載：
#   launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.jackm4.goldenstocks.pivot-wall-daily.plist
#   rm ~/Library/LaunchAgents/com.jackm4.goldenstocks.pivot-wall-daily.plist
#
# 看累積結果：
#   PYTHONPATH=src .venv/bin/python scripts/research/pivot_wall_daily.py --report

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}"
LOG="${STATE}/logs/pivot_wall_daily.log"

mkdir -p "${STATE}/logs"
exec >>"${LOG}" 2>&1

# 週二至週六才有前一個交易日可處理（週一會處理到週日＝沒有資料）
WD="$(date '+%u')"
if [[ "${WD}" -lt 2 || "${WD}" -gt 6 ]]; then
  echo "[$(date '+%F %T')] skip: weekday=${WD}（週二至週六才跑）"
  exit 0
fi

# 防重疊：PID 存活判定，不用時間老化
LOCKDIR="${STATE}/logs/.pivot-wall-daily.lockdir"
if ! mkdir "${LOCKDIR}" 2>/dev/null; then
  holder="$(cat "${LOCKDIR}/pid" 2>/dev/null || echo "")"
  if [[ -n "${holder}" ]] && kill -0 "${holder}" 2>/dev/null; then
    echo "[$(date '+%F %T')] skip: already running (pid=${holder})"
    exit 0
  fi
  rm -rf "${LOCKDIR}"; mkdir "${LOCKDIR}" 2>/dev/null || exit 0
fi
echo "$$" > "${LOCKDIR}/pid"
trap 'rm -rf "${LOCKDIR}" 2>/dev/null || true' EXIT

cd "${ROOT}"
echo "[$(date '+%F %T')] pivot-wall-daily start"
# rc=1 代表「一筆都沒寫」——假日、或收集器停擺，都是正常可能，不該讓 set -e
# 把整支腳本打死（也不該讓 launchd 記成失敗）。真正的錯誤 python 端已各自吞掉
# 並印出來，這裡只負責把 rc 留在 log 裡給人看。
rc=0
PYTHONPATH="${ROOT}/src" "${ROOT}/.venv/bin/python" \
  "${ROOT}/scripts/research/pivot_wall_daily.py" --roots TMF,MXF,TXF --sessions day,night || rc=$?
if [[ "${rc}" -eq 0 ]]; then
  echo "[$(date '+%F %T')] pivot-wall-daily done"
else
  echo "[$(date '+%F %T')] pivot-wall-daily done rc=${rc}（無新增紀錄：假日或收集器停擺）"
fi
