#!/usr/bin/env bash
# launchd：ops Live TA · 持倉∪extras · 盤中每 ~15s（plist StartInterval）
# 處置 20 分撮合僅 OPS_LIVE_TA_DISPOSITION；其餘為連續競價短動能。
# 現價＝Yahoo Chart regularMarketPrice（非委託限價）；短動能仍看 1m bars。
# 僅 mini 安裝；Book 不裝 live launchd。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_ops-live-ta-poll.log"

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
# 08:50–13:35 提醒窗（含開盤前）
if [[ "${H}" -lt 8 ]] || [[ "${H}" -gt 13 ]]; then
  echo "skip: outside window $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi
if [[ "${H}" -eq 8 && "${M}" -lt 50 ]]; then
  echo "skip: before 08:50 $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi
if [[ "${H}" -eq 13 && "${M}" -gt 35 ]]; then
  echo "skip: after 13:35 $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

echo "=== launchd ops-live-ta-poll $(date '+%Y-%m-%d %H:%M:%S') ==="
export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
cd "${ROOT}"

if [[ -r "${ROOT}/.env" ]]; then
  set +e
  set -a
  # shellcheck disable=SC1090
  source "${ROOT}/.env" 2>/dev/null
  set +a
  set -e
fi

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  echo "error: missing ${PY}"
  exit 1
fi

exec "${PY}" "${ROOT}/scripts/order/run_ops_live_ta_poll.py"
