#!/usr/bin/env bash
# 手動／除錯入口：TMF micro-channel Order poll（正式排程走 Application Support launcher）
# 需 .venv-fubon；實彈四鎖見 config/order.yaml · tmf-micro-channel

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export GOLDENSTOCKS_DATA_DIR="${GOLDENSTOCKS_DATA_DIR:-${HOME}/goldenstocks-data}"
export TZ="${TZ:-Asia/Taipei}"
export PYTHONPATH="${ROOT}/src"

LAUNCHD_LOG="${GOLDENSTOCKS_DATA_DIR}/logs/intraday/launchd_tmf-channel-poll.log"
mkdir -p "${GOLDENSTOCKS_DATA_DIR}/logs/intraday"
: >>"${LAUNCHD_LOG}"
exec >>"${LAUNCHD_LOG}" 2>&1

echo "=== manual tmf-channel-poll $(date '+%Y-%m-%d %H:%M:%S') ==="

PYTHON="${ROOT}/.venv-fubon/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "✗ missing .venv-fubon python: ${PYTHON}"
  exit 1
fi

exec "${PYTHON}" "${ROOT}/scripts/order/run_tmf_channel_poll.py" --json "$@"
