#!/usr/bin/env bash
# 個股期貨五檔＋逐筆收集器 · 常駐（KeepAlive）· **唯讀，無任何送單路徑**。
# 與 futopt-books-collect（指數核心 8 檔）分開跑，各持一條 websocket 連線。
#
# 2026-08-21 建立。起因：ROOTS 從 8 擴到 27 之後 108 個訂閱擠在一條連線上，當天 11:08
# 撞上「Maximum number of connections reached」，復原 2h48m / 48 個重啟循環，指數組
# 日盤掉了 157/300 分鐘（52%）。拆成兩條連線讓訂閱數對半分。
#
# 安裝（Mac mini，一次性，不透過 scripts/install-launchd.sh 的下單層陣列）：
#   PLIST=~/Library/LaunchAgents/com.jackm4.goldenstocks.futopt-books-collect-stocks.plist
#   sed -e "s#{{FUTOPT_BOOKS_COLLECT_STOCKS_LAUNCHER}}#$(pwd)/scripts/launchd/futopt-books-collect-stocks.command#g" \
#       -e "s#{{HOME}}#${HOME}#g" \
#       launchd/com.jackm4.goldenstocks.futopt-books-collect-stocks.plist.template > "${PLIST}"
#   launchctl bootstrap "gui/$(id -u)" "${PLIST}"
#
# 卸載：
#   launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.jackm4.goldenstocks.futopt-books-collect-stocks.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
APP_SUPPORT="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
STATE="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}"
WORKER_PY="${ROOT}/scripts/research/collect_stock_futures_books.py"

mkdir -p "${STATE}/logs/intraday" "${APP_SUPPORT}" "${HOME}/Library/Logs/com.jackm4.goldenstocks"
export TZ="${TZ:-Asia/Taipei}"
export PYTHONPATH="${ROOT}/src"

# --- 反重跑：PID 存活判定（與指數組同一套，但**獨立的** lockdir）---
LOCKDIR="${APP_SUPPORT}/futopt-books-collect-stocks.lockdir"
if ! mkdir "${LOCKDIR}" 2>/dev/null; then
  holder="$(cat "${LOCKDIR}/pid" 2>/dev/null || echo "")"
  if [[ -n "${holder}" ]] && kill -0 "${holder}" 2>/dev/null; then
    echo "skip: already holding lock (pid=${holder}, alive)"; exit 0
  fi
  rm -rf "${LOCKDIR}"; mkdir "${LOCKDIR}" 2>/dev/null || { echo "skip: lock race lost"; exit 0; }
fi
echo "$$" > "${LOCKDIR}/pid"
trap 'rm -rf "${LOCKDIR}" 2>/dev/null || true' EXIT

# 收殘骸：只殺這支 wrapper 的行程，**不要**碰指數組的 collect_ccf_books_websocket.py
REAP_PAT="collect_stock_futures_books.py"
for stale in $(pgrep -f "${REAP_PAT}" 2>/dev/null || true); do
  [[ "${stale}" == "$$" ]] && continue
  echo "reaping pre-existing pid=${stale}"; kill -TERM "${stale}" 2>/dev/null || true
done

_load_env_file() {
  [[ -r "$1" ]] || return 0
  set +e; set -a; source "$1" 2>/dev/null; local rc=$?; set +a; set -e
  [[ "${rc}" -ne 0 ]] && echo "WARN: cannot source $2 rc=${rc}"
  return 0
}
_load_env_file "${APP_SUPPORT}/order.env" "order.env"
_load_env_file "${STATE}/.env" "project .env"

PYTHON="${ROOT}/.venv-fubon/bin/python"
if [[ ! -x "${PYTHON}" ]]; then echo "✗ missing .venv-fubon python: ${PYTHON}"; exit 1; fi
ROTATING_TEE="${ROOT}/scripts/launchd/rotating_tee.py"
LOG_PREFIX="${STATE}/logs/intraday/futopt_books_collect_stocks"

exec "${PYTHON}" "${WORKER_PY}" 2>&1 | "${PYTHON}" "${ROTATING_TEE}" "${LOG_PREFIX}"
