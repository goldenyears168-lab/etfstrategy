#!/usr/bin/env bash
# launchd：週一至五 08:28 觸發一次，腳本內部等到 08:29:00 開始輪詢，跑到 09:01:00 自行結束
# 研究層資料收集（scripts/research/），非下單層，不下單、不改倉、不寄信。
# 手動除錯：open -gj scripts/launchd/fubon-premarket-quote-collect.command
#
# 安裝（Mac mini，一次性，不透過 scripts/install-launchd.sh 的下單層陣列）：
#   PLIST=~/Library/LaunchAgents/com.jackm4.goldenstocks.fubon-premarket-quote-collect.plist
#   sed -e "s#{{FUBON_PREMARKET_QUOTE_COLLECT_LAUNCHER}}#$(pwd)/scripts/launchd/fubon-premarket-quote-collect.command#g" \
#       -e "s#{{HOME}}#${HOME}#g" \
#       launchd/com.jackm4.goldenstocks.fubon-premarket-quote-collect.plist.template > "${PLIST}"
#   launchctl bootstrap "gui/$(id -u)" "${PLIST}"   # 或 launchctl load "${PLIST}"
#
# 卸載：
#   launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.jackm4.goldenstocks.fubon-premarket-quote-collect.plist
#   rm ~/Library/LaunchAgents/com.jackm4.goldenstocks.fubon-premarket-quote-collect.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday/launchd_fubon-premarket-quote-collect.log"

mkdir -p "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday"
: >>"${LAUNCHD_LOG}"

exec >>"${LAUNCHD_LOG}" 2>&1

WD="$(date '+%u')"
# 1=Mon…5=Fri；週末不跑（StartCalendarInterval 已限定週一至五，這裡是防呆）
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd fubon-premarket-quote-collect $(date '+%Y-%m-%d %H:%M:%S') ==="

export PYTHONPATH="${ROOT}/src"
if [[ -f "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/.env" ]]; then
  set +e
  set -a
  # shellcheck disable=SC1091
  source "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/.env"
  set +a
  set -e
fi

PYTHON="${ROOT}/.venv-fubon/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv-fubon python: ${PYTHON}"
  exit 1
fi

set +e
"${PYTHON}" "${ROOT}/scripts/research/collect_fubon_premarket_quote.py"
EXIT=$?
set -e

echo "=== launchd fubon-premarket-quote-collect end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
