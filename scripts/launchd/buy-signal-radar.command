#!/usr/bin/env bash
# launchd：Buy signal radar · 09:00–13:20 每 5 分 · observe advisory email only
# Live Order sleeves：C18acc（rrg poll）+ Leading Dip（leading-dip-poll）。
# ABC Order 已退役（2026-07-15）· radar 不再送單。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_buy-signal-radar.log"
EXIT=0

mkdir -p "${ROOT}/logs/intraday"
: >>"${LAUNCHD_LOG}"

exec >>"${LAUNCHD_LOG}" 2>&1

WD="$(date '+%u')"
H=$((10#$(date '+%H')))
M=$((10#$(date '+%M')))
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi
if [[ "${H}" -lt 9 ]] || [[ "${H}" -gt 13 ]] || { [[ "${H}" -eq 13 ]] && [[ "${M}" -gt 20 ]]; }; then
  echo "skip: outside window $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd buy-signal-radar tick $(date '+%Y-%m-%d %H:%M:%S') ==="

export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export RUN_BUY_SIGNAL_RADAR="${RUN_BUY_SIGNAL_RADAR:-1}"
export RUN_BUY_SIGNAL_EMAIL="${RUN_BUY_SIGNAL_EMAIL:-1}"
export C18ACC_KBAR_SYNC="${C18ACC_KBAR_SYNC:-1}"
export SIGNAL_RADAR_USE_FUBON="${SIGNAL_RADAR_USE_FUBON:-1}"

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing venv python: ${PYTHON}"
  exit 1
fi

set +e
OUT="$("${PYTHON}" "${ROOT}/scripts/run_buy_signal_radar.py" 2>&1)"
EXIT=$?
set -e
echo "${OUT}"

# 直接呼叫 venv python 寄信（勿 exec Documents/*.sh · launchd TCC 會擋）
_notify_buy() {
  local intro="$1"
  local env_flag="$2"
  local pattern="$3"
  local EXTRA_LINES EXTRA rel STAMP
  STAMP="$(date '+%Y%m%d')"
  EXTRA_LINES="$(echo "${OUT}" | grep -E "${pattern}" || true)"
  EXTRA="${intro}"$'\n'"${EXTRA_LINES}"$'\n'
  for rel in \
    "reports/daily/${STAMP}_buy_signal_radar.md" \
    "reports/daily/buy_signal_radar.md"; do
    [[ -f "${ROOT}/${rel}" ]] && EXTRA+="${rel}"$'\n'
  done
  set +e
  "${PYTHON}" "${ROOT}/scripts/notify_job_result.py" \
    --subject-prefix="Buy signal radar" \
    --exit-code="${EXIT}" \
    --log-path="${LAUNCHD_LOG}" \
    --extra="${EXTRA}" \
    --env-flag="${env_flag}"
  set -e
}

if echo "${OUT}" | grep -q 'BUY_SIGNAL_RADAR=1'; then
  _notify_buy \
    $'買進 / observe 命中（ABC Order 已退役；radar 僅通知）' \
    "RUN_BUY_SIGNAL_EMAIL" \
    'BUY_SIGNAL_RADAR|買進|觀測命中|多軌重疊|· 略過|buy '
fi

echo "=== launchd buy-signal-radar end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
