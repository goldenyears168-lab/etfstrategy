#!/usr/bin/env bash
# launchd：ops Live TA · 長駐迴圈（週一至五 08:50 觸發一次，腳本跑到 13:35）
# 一輪登入富邦，進程內 TTL（1m≈45s／5m≈90s）才真正生效；現價仍約每 15s。
# Universe = 富邦持倉同步（SQLite∪ops.holdings）∪ OPS_LIVE_TA_STOCKS extras。
# 處置 20 分撮合僅 OPS_LIVE_TA_DISPOSITION。必須 .venv-fubon。僅 mini。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LAUNCHD_LOG="${ROOT}/logs/intraday/launchd_ops-live-ta-poll.log"

mkdir -p "${ROOT}/logs/intraday"
: >>"${LAUNCHD_LOG}"
exec >>"${LAUNCHD_LOG}" 2>&1

WD="$(date '+%u')"
if [[ "${WD}" -gt 5 ]]; then
  echo "skip: weekend $(date '+%Y-%m-%d %H:%M:%S')"
  exit 0
fi

# Portable lock (macOS often has no flock) · avoid double long-running sessions
LOCK_DIR="${ROOT}/logs/intraday/ops-live-ta-poll.lockdir"
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  # Stale lock: if no live python loop, reclaim
  if ! pgrep -f "run_ops_live_ta_poll.py --loop" >/dev/null 2>&1; then
    rmdir "${LOCK_DIR}" 2>/dev/null || true
    mkdir "${LOCK_DIR}" 2>/dev/null || {
      echo "skip: already running $(date '+%Y-%m-%d %H:%M:%S')"
      exit 0
    }
  else
    echo "skip: already running $(date '+%Y-%m-%d %H:%M:%S')"
    exit 0
  fi
fi
trap 'rmdir "${LOCK_DIR}" 2>/dev/null || true' EXIT

echo "=== launchd ops-live-ta-poll LOOP start $(date '+%Y-%m-%d %H:%M:%S') ==="
export ROOT="${ROOT}"
export PYTHONPATH="${ROOT}/src"
export PYTHONUNBUFFERED=1
cd "${ROOT}"

if [[ -r "${ROOT}/.env" ]]; then
  set +e
  set -a
  # shellcheck disable=SC1090
  source "${ROOT}/.env" 2>/dev/null
  set +a
  set -e
fi

PY="${ROOT}/.venv-fubon/bin/python"
if [[ ! -x "${PY}" ]]; then
  echo "warn: missing ${PY} · fallback .venv (Yahoo quotes)"
  PY="${ROOT}/.venv/bin/python"
  export OPS_LIVE_TA_QUOTE="${OPS_LIVE_TA_QUOTE:-yahoo}"
fi
if [[ ! -x "${PY}" ]]; then
  echo "error: missing python at .venv-fubon and .venv"
  exit 1
fi

INTERVAL="${OPS_LIVE_TA_INTERVAL_SEC:-15}"
START_TIME="${OPS_LIVE_TA_START:-08:50:00}"
END_TIME="${OPS_LIVE_TA_END:-13:35:00}"

set +e
"${PY}" "${ROOT}/scripts/order/run_ops_live_ta_poll.py" \
  --loop \
  --interval-sec "${INTERVAL}" \
  --start-time "${START_TIME}" \
  --end-time "${END_TIME}"
EXIT=$?
set -e

echo "=== launchd ops-live-ta-poll LOOP end exit=${EXIT} $(date '+%Y-%m-%d %H:%M:%S') ==="
exit "${EXIT}"
