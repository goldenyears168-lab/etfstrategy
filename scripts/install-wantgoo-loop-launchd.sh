#!/usr/bin/env bash
# 安裝「每日情報迴圈」研究層 launchd（不下單、不碰 order layer）：
#   com.jackm4.goldenstocks.chip-macro-tracker-daily  週一至五 18:00
#       daily_tracker: refresh panel + 6燈 + 資料新鮮度gate + 隔日驗證backfill + ops推送
#   com.jackm4.goldenstocks.wantgoo-loop-daily        週一至五 20:15
#       fetch 白名單6位新文 + 記分板 score + 擷取(有金鑰API/無金鑰匯出bundle)
#
# 刻意獨立於 install-launchd.sh（那支管 live order layer，不宜混改）。
# log: stdout→ ~/Library/Logs/com.jackm4.goldenstocks/（避開 Documents TCC → EX_CONFIG(78)）
#      run→ ${GOLDENSTOCKS_DATA_DIR:-ROOT}/logs/
#
# 用法:
#   scripts/install-wantgoo-loop-launchd.sh            # 渲染+載入
#   scripts/install-wantgoo-loop-launchd.sh --uninstall
#   scripts/install-wantgoo-loop-launchd.sh --status
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SRC="${PROJECT_ROOT}/launchd"
AGENT_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"

LABELS=(
  com.jackm4.goldenstocks.chip-macro-tracker-daily
  com.jackm4.goldenstocks.wantgoo-loop-daily
)

mode="${1:-install}"

status() {
  for label in "${LABELS[@]}"; do
    if launchctl print "${GUI_DOMAIN}/${label}" >/dev/null 2>&1; then
      echo "  ● loaded   ${label}"
    else
      echo "  ○ not-loaded ${label}"
    fi
  done
}

uninstall() {
  for label in "${LABELS[@]}"; do
    launchctl bootout "${GUI_DOMAIN}/${label}" 2>/dev/null || true
    rm -f "${AGENT_DIR}/${label}.plist"
    echo "  ✗ removed ${label}"
  done
}

install() {
  mkdir -p "${AGENT_DIR}" "${HOME}/Library/Logs/com.jackm4.goldenstocks"
  for label in "${LABELS[@]}"; do
    local tmpl="${SRC}/${label}.plist.template"
    local dst="${AGENT_DIR}/${label}.plist"
    if [[ ! -f "${tmpl}" ]]; then echo "✗ 缺少 template ${tmpl}" >&2; exit 1; fi
    sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" -e "s|{{HOME}}|${HOME}|g" \
        "${tmpl}" > "${dst}"
    chmod +x "${PROJECT_ROOT}/scripts/launchd/${label#com.jackm4.goldenstocks.}.command" 2>/dev/null || true
    launchctl bootout "${GUI_DOMAIN}/${label}" 2>/dev/null || true
    launchctl bootstrap "${GUI_DOMAIN}" "${dst}"
    echo "  ✓ installed+loaded ${label}"
  done
  echo ""
  echo "手動測試（不等排程）:"
  echo "  launchctl kickstart -k ${GUI_DOMAIN}/com.jackm4.goldenstocks.chip-macro-tracker-daily"
  echo "  launchctl kickstart -k ${GUI_DOMAIN}/com.jackm4.goldenstocks.wantgoo-loop-daily"
}

case "${mode}" in
  --uninstall) uninstall ;;
  --status)    status ;;
  install|"")  install ;;
  *) echo "用法: $(basename "$0") [--uninstall|--status]" >&2; exit 1 ;;
esac
