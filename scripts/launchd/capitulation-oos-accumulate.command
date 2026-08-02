#!/usr/bin/env bash
# launchd:每工作日 19:00 · 投降抄底策略前瞻 OOS 帳本累積 (signal_capitulation_forward)。
# 量測/研究層:不下單、不改倉。冪等,每日重跑自動補「已成熟」的前瞻報酬,累積樣本外事件。
# 手動除錯: open -gj scripts/launchd/capitulation-oos-accumulate.command
# 卸載: launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.jackm4.goldenstocks.capitulation-oos-accumulate.plist
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
LOG_DIR="${HOME}/Library/Logs/com.jackm4.goldenstocks"
RUN_LOG="${LOG_DIR}/capitulation_oos_accumulate_${STAMP}.log"
mkdir -p "${LOG_DIR}"
export PYTHONPATH="${ROOT}/src"
PYTHON="${ROOT}/.venv/bin/python"
[[ -x "${PYTHON}" ]] || PYTHON="python3"
echo "=== capitulation-oos-accumulate 開始 $(date '+%F %T') ===" | tee -a "${RUN_LOG}"
set +e
"${PYTHON}" "${ROOT}/src/capitulation_forward_ledger.py" --db "${HOME}/goldenstocks-data/data/stocks.db" 2>&1 | tee -a "${RUN_LOG}"
EXITC=${PIPESTATUS[0]}
set -e
echo "=== 結束 exit=${EXITC} $(date '+%F %T') ===" | tee -a "${RUN_LOG}"
exit "${EXITC}"
