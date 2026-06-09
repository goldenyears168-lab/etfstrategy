#!/usr/bin/env bash
# 安裝方案 C launchd 排程（① 08:30 · ② 16:30 · ③ 週日 20:00）
#
# 用法：
#   scripts/install-launchd.sh           # 安裝並載入
#   scripts/install-launchd.sh --uninstall
#   scripts/install-launchd.sh --status

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCHD_SRC="${PROJECT_ROOT}/launchd"
AGENT_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"

LABELS=(
  com.jackm4.etf.execution-eval
  com.jackm4.etf.evening-holdings
  com.jackm4.etf.weekly-deep
)

TEMPLATES=(
  com.jackm4.etf.execution-eval.plist.template
  com.jackm4.etf.evening-holdings.plist.template
  com.jackm4.etf.weekly-deep.plist.template
)

usage() {
  cat <<EOF
用法: $(basename "$0") [--uninstall|--status]

  預設：將 launchd/*.plist.template 渲染後安裝到
        ~/Library/LaunchAgents/ 並 launchctl load。

  排程（本地時間）：
    ① execution-eval    週一至五 08:30
    ② evening-holdings  週一至五 16:30
    ③ weekly-deep       週日     20:00

  log：${PROJECT_ROOT}/logs/launchd_*.log

  注意：Mac 須已登入；睡眠中可能不觸發。
        系統設定 → 隱私權 → 完整磁碟取用權限（Terminal 等）。
EOF
}

bootout_label() {
  local label="$1"
  launchctl bootout "${GUI_DOMAIN}/${label}" 2>/dev/null || true
  launchctl unload "${AGENT_DIR}/${label}.plist" 2>/dev/null || true
}

bootstrap_label() {
  local plist_path="$1"
  local label
  label="$(basename "${plist_path}" .plist)"
  if launchctl bootstrap "${GUI_DOMAIN}" "${plist_path}" 2>/dev/null; then
    return 0
  fi
  launchctl load "${plist_path}"
}

render_template() {
  local template="$1"
  local dest="$2"
  sed "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" "${template}" >"${dest}"
}

install_agents() {
  if [[ ! -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
    echo "✗ 找不到 ${PROJECT_ROOT}/.venv/bin/python" >&2
    echo "  請先在專案根目錄建立 venv 並安裝 requirements.txt" >&2
    exit 1
  fi

  mkdir -p "${AGENT_DIR}" "${PROJECT_ROOT}/logs"

  echo "專案：${PROJECT_ROOT}"
  echo "安裝至：${AGENT_DIR}"
  echo ""

  local i template src dest label
  for i in "${!TEMPLATES[@]}"; do
    template="${TEMPLATES[$i]}"
    label="${LABELS[$i]}"
    src="${LAUNCHD_SRC}/${template}"
    dest="${AGENT_DIR}/${label}.plist"

    if [[ ! -f "${src}" ]]; then
      echo "✗ 缺少範本 ${src}" >&2
      exit 1
    fi

    bootout_label "${label}"
    render_template "${src}" "${dest}"
    bootstrap_label "${dest}"
    echo "✓ ${label}"
  done

  echo ""
  echo "完成。檢查："
  echo "  launchctl list | grep jackm4.etf"
  echo "  tail -f ${PROJECT_ROOT}/logs/launchd_execution-eval.log"
  echo "  tail -f ${PROJECT_ROOT}/logs/daily_sync_\$(date +%Y%m%d).log"
}

uninstall_agents() {
  local label dest
  for label in "${LABELS[@]}"; do
    dest="${AGENT_DIR}/${label}.plist"
    bootout_label "${label}"
    if [[ -f "${dest}" ]]; then
      rm -f "${dest}"
      echo "✓ 已移除 ${dest}"
    fi
  done
  echo "卸載完成。"
}

show_status() {
  echo "LaunchAgents："
  launchctl list 2>/dev/null | grep -E 'jackm4\.etf' || echo "  （無已載入的 com.jackm4.etf.*）"
  echo ""
  echo "plist 檔案："
  local label
  for label in "${LABELS[@]}"; do
    if [[ -f "${AGENT_DIR}/${label}.plist" ]]; then
      echo "  ✓ ${AGENT_DIR}/${label}.plist"
    else
      echo "  — ${label}.plist（未安裝）"
    fi
  done
}

ACTION=install
case "${1:-}" in
  --uninstall) ACTION=uninstall ;;
  --status) ACTION=status ;;
  -h|--help) usage; exit 0 ;;
  "") ACTION=install ;;
  *)
    echo "未知參數：$1" >&2
    usage
    exit 2
    ;;
esac

case "${ACTION}" in
  install) install_agents ;;
  uninstall) uninstall_agents ;;
  status) show_status ;;
esac
