#!/usr/bin/env bash
# limitup-fade 每晚選股信 · 平日 22:45 · **唯讀、無送單路徑**。
#
# 2026-09-03 建立（實單前測第一步）。策略：T 日漲停鎖死 → T+1 開盤集合競價放空吃
# 遞延買盤釋放（真 OOS 2020~2026-02 每筆 +0.79% cluster-t=5.07、日均淨 +0.663%、
# Sharpe 2.43、七年逐年皆正；SSOT＝scripts/research/screen_limitup_fade.py 檔頭）。
# 前測目的：讓隔日 08:30 有名單可做人工四確認（開盤未鎖／可當沖／券源／處置），
# 並累積 reports/research/limitup-fade/*.md 成前瞻紀錄。**本 job 不下單。**
#
# 為何 22:45：篩選依賴 stock_daily_bars source='twse_mi_index'，該源晚間才落庫
# （實測 19:08 仍只有前一日）；22:45 也錯開 22:15/22:30 兩支 FinMind job 的 quota。
# 資料未更新時信件主旨帶 ⚠，名單仍寄（用前一日資料＝已過期，僅供對帳）。
#
# 安裝（Mac mini，一次性，不透過 scripts/install-launchd.sh 的 LABELS 陣列）：
#   PLIST=~/Library/LaunchAgents/com.jackm4.goldenstocks.limitup-fade-nightly.plist
#   sed -e "s#{{LIMITUP_FADE_NIGHTLY_LAUNCHER}}#$(pwd)/scripts/launchd/limitup-fade-nightly.command#g" \
#       -e "s#{{HOME}}#${HOME}#g" \
#       launchd/com.jackm4.goldenstocks.limitup-fade-nightly.plist.template > "${PLIST}"
#   launchctl bootstrap "gui/$(id -u)" "${PLIST}"
# 卸載：
#   launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.jackm4.goldenstocks.limitup-fade-nightly.plist

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE="${GOLDENSTOCKS_DATA_DIR:-${ROOT}}"
export TZ="Asia/Taipei" PYTHONPATH="${ROOT}/src"

case "$(date +%u)" in 6|7) echo "weekend, skip"; exit 0;; esac

set +e; set -a; source "${STATE}/.env" 2>/dev/null; set +a; set -e
if [[ "${RUN_LIMITUP_FADE_NIGHTLY:-1}" != "1" ]]; then echo "RUN_LIMITUP_FADE_NIGHTLY!=1, skip"; exit 0; fi

STAMP="$(date +%Y%m%d)"
LOG="${STATE}/logs/limitup_fade_nightly_${STAMP}.log"
mkdir -p "${STATE}/logs"
PY="${ROOT}/.venv/bin/python"
{
  echo "=== limitup-fade nightly $(date '+%F %T') ==="
  # 條件 5 的處置名單快照（失敗不擋主流程，但要留痕）
  "${PY}" "${ROOT}/scripts/research/fetch_disposal_list.py" 2>&1 || echo "WARN: fetch_disposal_list 失敗，處置過濾可能不完整"
  "${PY}" "${ROOT}/scripts/research/screen_limitup_fade.py" 2>&1
  RC=$?
  echo "=== screen exit=${RC} ==="
} | tee -a "${LOG}"

OUT_DIR="${ROOT}/reports/research/limitup-fade"
LATEST="$(ls -t "${OUT_DIR}"/2*.md 2>/dev/null | head -1 || true)"
if [[ -z "${LATEST}" ]]; then echo "BLOCKER: 無產出檔"; exit 1; fi
T="$(basename "${LATEST}" .md)"
TODAY="$(date +%F)"
SUBJ="limitup-fade 隔日放空名單 T=${T}"
if [[ "${T}" != "${TODAY}" ]]; then SUBJ="⚠資料未更新(僅到 ${T}) limitup-fade"; fi
"${PY}" - "${SUBJ}" "${LATEST}" "${LOG}" <<'PYEOF'
import sys
from notify_email import send_alert
subj, md, log = sys.argv[1], sys.argv[2], sys.argv[3]
body = open(md, encoding="utf-8").read()
body += ("\n\n---\n【隔日 08:30 人工四確認】試撮未鎖漲停／可現股當沖(先賣後買)／券源／不在處置分盤\n"
         "【定位】實單前測 · 本信不構成委託；每日等權是規則的一部分\n"
         f"log: {log}\n")
send_alert(subj, body)
print(f"mailed: {subj}")
PYEOF
