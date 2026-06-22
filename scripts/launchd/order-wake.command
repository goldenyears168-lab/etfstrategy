#!/usr/bin/env bash
# launchd：週一至五 08:55 喚醒／防睡眠 10 分鐘，確保 09:00 送單可執行

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LOG="${ROOT}/logs/launchd_order-wake.log"

exec >>"${LOG}" 2>&1
echo "=== launchd order-wake $(date '+%Y-%m-%d %H:%M:%S') ==="

# 若已在醒著狀態，caffeinate 僅短暫防止再睡；睡眠中需靠 pmset 排程喚醒
/usr/bin/caffeinate -i -t 600 &
echo "caffeinate -i -t 600 started pid=$!"

echo "=== launchd order-wake 結束 ==="
exit 0
