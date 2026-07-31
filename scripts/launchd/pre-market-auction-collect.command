#!/usr/bin/env bash
# launchd：週一至五 08:29-09:01 每 60 秒觸發一次，收集全市場盤前試撮 tick snapshot
# 研究層資料收集（scripts/research/），非下單層，不下單、不改倉、不寄信。
# 手動除錯：open -gj scripts/launchd/pre-market-auction-collect.command
#
# 安裝（Mac mini，一次性，不透過 scripts/install-launchd.sh 的下單層陣列）：
#   PLIST=~/Library/LaunchAgents/com.jackm4.goldenstocks.pre-market-auction-collect.plist
#   sed -e "s#{{PRE_MARKET_AUCTION_COLLECT_LAUNCHER}}#$(pwd)/scripts/launchd/pre-market-auction-collect.command#g" \
#       -e "s#{{HOME}}#${HOME}#g" \
#       launchd/com.jackm4.goldenstocks.pre-market-auction-collect.plist.template > "${PLIST}"
#   launchctl bootstrap "gui/$(id -u)" "${PLIST}"   # 或 launchctl load "${PLIST}"
#
# 卸載：
#   launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.jackm4.goldenstocks.pre-market-auction-collect.plist
#   rm ~/Library/LaunchAgents/com.jackm4.goldenstocks.pre-market-auction-collect.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday/launchd_pre-market-auction-collect.log"
EXIT=0

mkdir -p "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday"
: >>"${LAUNCHD_LOG}"

exec >>"${LAUNCHD_LOG}" 2>&1

WD="$(date '+%u')"
H=$((10#$(date '+%H')))
M=$((10#$(date '+%M')))
# 1=Mon…5=Fri；週末不跑
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi
# 08:29-09:01（含尾端緩衝，捕捉 08:59:55 鎖單瞬間與 09:00 開盤瞬間）
if [[ "${H}" -eq 8 && "${M}" -ge 29 ]]; then
  :
elif [[ "${H}" -eq 9 && "${M}" -le 1 ]]; then
  :
else
  echo "skip: outside 08:29-09:01 window $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd pre-market-auction-collect $(date '+%Y-%m-%d %H:%M:%S') ==="

export PYTHONPATH="${ROOT}/src"
if [[ -f "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/.env" ]]; then
  set +e
  set -a
  # shellcheck disable=SC1091
  source "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/.env"
  set +a
  set -e
fi

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/research/collect_pre_market_auction_snapshot.py" \
  --all-market --skip-market-stats --skip-bidask-probe 2>&1)"
EXIT=$?
set -e
echo "${OUT}"

echo "=== launchd pre-market-auction-collect end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
