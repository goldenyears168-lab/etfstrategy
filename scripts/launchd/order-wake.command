#!/usr/bin/env bash
# 盤中防休眠：鐘面每 5 分觸發；Mon–Fri 08:50–13:40 續命 caffeinate。
# 背景：純 08:55×10 分不足以撐完整個盤中；Aqua CalendarInterval 在螢幕休眠後易停火。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LOG="${ROOT}/logs/launchd_order-wake.log"
mkdir -p "${ROOT}/logs"

exec >>"${LOG}" 2>&1
echo "=== launchd order-wake $(date '+%Y-%m-%d %H:%M:%S') ==="

WD="$(date '+%u')"
H=$((10#$(date '+%H')))
M=$((10#$(date '+%M')))

# Mon–Fri 08:50–13:40
in_window=0
if [[ "${WD}" -le 5 ]]; then
  if [[ "${H}" -eq 8 && "${M}" -ge 50 ]]; then
    in_window=1
  elif [[ "${H}" -ge 9 && "${H}" -le 12 ]]; then
    in_window=1
  elif [[ "${H}" -eq 13 && "${M}" -le 40 ]]; then
    in_window=1
  fi
fi

if [[ "${in_window}" -ne 1 ]]; then
  echo "skip: outside market keep-awake window"
  echo "=== launchd order-wake 結束 ==="
  exit 0
fi

# -d display  -i idle  -m disk  -s system  -u user；續 6 分（下一格再續）
/usr/bin/caffeinate -dimsu -t 360 &
echo "caffeinate -dimsu -t 360 started pid=$!"
echo "=== launchd order-wake 結束 ==="
exit 0
