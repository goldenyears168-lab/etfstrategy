#!/usr/bin/env bash
# launchd：每週六 08:00 · 刷新 TWSE「第二次處置」清單 + 累積處置股專家池 OOS 凍結評分。
# 研究層（scripts/research/），非下單層：不下單、不改倉；預設不寄信（僅硬失敗通知）。
# 手動除錯：open -gj scripts/launchd/second-disp-oos-accumulate.command
# 知識庫：reports/research/branch-footprint-screen/expanded/BLINDSPOT_RECHECK_AND_OOS_FREEZE.md
#
# 安裝（Mac mini，一次性，不透過 scripts/install-launchd.sh 的下單層陣列）：
#   PLIST=~/Library/LaunchAgents/com.jackm4.etf.second-disp-oos-accumulate.plist
#   sed -e "s#{{SECOND_DISP_OOS_LAUNCHER}}#$(pwd)/scripts/launchd/second-disp-oos-accumulate.command#g" \
#       -e "s#{{PROJECT_ROOT}}#$(pwd)#g" \
#       launchd/com.jackm4.etf.second-disp-oos-accumulate.plist.template > "${PLIST}"
#   launchctl bootstrap "gui/$(id -u)" "${PLIST}"
#
# 卸載：
#   launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.jackm4.etf.second-disp-oos-accumulate.plist
#   rm ~/Library/LaunchAgents/com.jackm4.etf.second-disp-oos-accumulate.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAMP="$(date '+%Y%m%d')"
RUN_LOG="${ROOT}/logs/second_disp_oos_accumulate_${STAMP}.log"
mkdir -p "${ROOT}/logs"

echo "" | tee -a "${RUN_LOG}"
echo "=== launchd second-disp-oos-accumulate 開始 $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "${RUN_LOG}"

export PYTHONPATH="${ROOT}/src:${ROOT}/scripts/research"
PYTHON="${ROOT}/.venv/bin/python"

if [[ -r "${ROOT}/.env" ]]; then
  set +e; set -a; # shellcheck disable=SC1091
  source "${ROOT}/.env" 2>/dev/null; set +a; set -e
fi

if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}" | tee -a "${RUN_LOG}"; exit 1
fi

# 1) 刷新 TWSE 第二次處置清單（rolling endDate）。抓取失敗會保留舊快取、非 0 退出碼，
#    但不阻斷 OOS（仍用既有快取跑），故此步失敗只記 log、不整體失敗。
set +e
"${PYTHON}" "${ROOT}/scripts/research/refresh_twse_disposition_cache.py" 2>&1 | tee -a "${RUN_LOG}"
REFRESH_EXIT=${PIPESTATUS[0]}
set -e
[[ "${REFRESH_EXIT}" -ne 0 ]] && echo "⚠ refresh exit=${REFRESH_EXIT}（用既有快取續跑 OOS）" | tee -a "${RUN_LOG}"

# 2) 累積 OOS 凍結評分（不 --refreeze；名單以 freeze_manifest 為準）。
set +e
"${PYTHON}" "${ROOT}/scripts/research/oos_freeze_score_expert_pool_seats.py" 2>&1 | tee -a "${RUN_LOG}"
OOS_EXIT=${PIPESTATUS[0]}
set -e

echo "=== launchd second-disp-oos-accumulate 結束 refresh=${REFRESH_EXIT} oos=${OOS_EXIT} $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "${RUN_LOG}"

# 只有 OOS 腳本本身硬失敗才通知（研究層，避免 email 噪音）
if [[ "${OOS_EXIT}" -ne 0 ]]; then
  set +e
  "${PYTHON}" "${ROOT}/scripts/notify_job_result.py" \
    --subject-prefix="處置股OOS累積" \
    --exit-code="${OOS_EXIT}" \
    --log-path="${RUN_LOG}" \
    --extra="OOS 凍結評分失敗，詳見 log" \
    --env-flag="RUN_SECOND_DISP_EXPERT_POOL_EMAIL" 2>/dev/null
  set -e
fi

exit "${OOS_EXIT}"
