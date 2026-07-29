#!/usr/bin/env bash
# launchd：週一至五 ~08:59 觸發一次，腳本內部 09:00–13:31 輪詢五檔＋成交
# 研究層資料收集（scripts/research/），非下單層，不下單、不改倉、不寄信。
#
# 標的（研究用 LOB 軌跡）：2492 華新科 · 2327 國巨 · 8046 南電 · 2330 · 0050
#
# 手動除錯：
#   open -gj scripts/launchd/fubon-intraday-quote-collect.command
#
# 安裝（Mac mini，一次性，不透過 scripts/install-launchd.sh 的下單層陣列）：
#   PLIST=~/Library/LaunchAgents/com.jackm4.etf.fubon-intraday-quote-collect.plist
#   sed -e "s#{{FUBON_INTRADAY_QUOTE_COLLECT_LAUNCHER}}#$(pwd)/scripts/launchd/fubon-intraday-quote-collect.command#g" \
#       -e "s#{{HOME}}#${HOME}#g" \
#       launchd/com.jackm4.etf.fubon-intraday-quote-collect.plist.template > "${PLIST}"
#   launchctl bootstrap "gui/$(id -u)" "${PLIST}"
#
# 卸載：
#   launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.jackm4.etf.fubon-intraday-quote-collect.plist
#   rm ~/Library/LaunchAgents/com.jackm4.etf.fubon-intraday-quote-collect.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday/launchd_fubon-intraday-quote-collect.log"

mkdir -p "${GOLDENSTOCKS_DATA_DIR:-${ROOT}}/logs/intraday"
: >>"${LAUNCHD_LOG}"

exec >>"${LAUNCHD_LOG}" 2>&1

WD="$(date '+%u')"
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd fubon-intraday-quote-collect $(date '+%Y-%m-%d %H:%M:%S') ==="

export PYTHONPATH="${ROOT}/src"
export PYTHONUNBUFFERED=1
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

# 2492 華新科 · 2327 國巨 · 8046 南電 · 2330 台積電 · 0050
SYMBOLS="${FUBON_INTRADAY_QUOTE_SYMBOLS:-2492,2327,8046,2330,0050}"
INTERVAL="${FUBON_INTRADAY_QUOTE_INTERVAL_SEC:-2}"
START_TIME="${FUBON_INTRADAY_QUOTE_START:-09:00:00}"
END_TIME="${FUBON_INTRADAY_QUOTE_END:-13:31:00}"

set +e
"${PYTHON}" "${ROOT}/scripts/research/collect_fubon_premarket_quote.py" \
  --symbols "${SYMBOLS}" \
  --start-time "${START_TIME}" \
  --end-time "${END_TIME}" \
  --interval-sec "${INTERVAL}"
EXIT=$?
set -e

echo "=== launchd fubon-intraday-quote-collect end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
