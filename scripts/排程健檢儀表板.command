#!/usr/bin/env bash
# 點擊開啟：launchd 排程時間 + 執行狀況健檢儀表板（唯讀）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}"

clear 2>/dev/null || true

"${ROOT}/scripts/launchd_status_dashboard.sh"

echo ""
read -r -p "按 Enter 關閉視窗…"
