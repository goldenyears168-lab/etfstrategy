#!/usr/bin/env bash
# 安裝方案 C launchd 排程（② 16:30 · ③ 週日 20:00）
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
  com.jackm4.etf.morning-holdings-brief
  com.jackm4.etf.intraday-exit-gate
  com.jackm4.etf.evening-holdings
  com.jackm4.etf.mutual-fund-disclosure-watch
  com.jackm4.etf.rrg-c18acc-poll
  com.jackm4.etf.buy-signal-radar
  com.jackm4.etf.sell-signal-radar
  com.jackm4.etf.rrg-mono-intraday-watch
  com.jackm4.etf.intraday-open-digest
  com.jackm4.etf.intraday-midday-digest
  com.jackm4.etf.intraday-1300-digest
  com.jackm4.etf.vcp-funnel-specs
  com.jackm4.etf.minervini-sepa-basket
  com.jackm4.etf.weekly-deep
)

TEMPLATES=(
  com.jackm4.etf.morning-holdings-brief.plist.template
  com.jackm4.etf.intraday-exit-gate.plist.template
  com.jackm4.etf.evening-holdings.plist.template
  com.jackm4.etf.mutual-fund-disclosure-watch.plist.template
  com.jackm4.etf.rrg-c18acc-poll.plist.template
  com.jackm4.etf.buy-signal-radar.plist.template
  com.jackm4.etf.sell-signal-radar.plist.template
  com.jackm4.etf.rrg-mono-intraday-watch.plist.template
  com.jackm4.etf.intraday-open-digest.plist.template
  com.jackm4.etf.intraday-midday-digest.plist.template
  com.jackm4.etf.intraday-1300-digest.plist.template
  com.jackm4.etf.vcp-funnel-specs.plist.template
  com.jackm4.etf.minervini-sepa-basket.plist.template
  com.jackm4.etf.weekly-deep.plist.template
)

usage() {
  cat <<EOF
用法: $(basename "$0") [--uninstall|--status]

  預設：將 launchd/*.plist.template 渲染後安裝到
        ~/Library/LaunchAgents/ 並 launchctl load。

  排程（本地時間）：
    ② evening-holdings      週一至五 16:30（持股 + RRG close + Improving watch + VCP close + stock_daily_lens → Supabase）
    ⑧ morning-holdings-brief 週一至五 08:50（富邦持倉 · 觸發價 · 台指 gap · DB snapshot · 不寄信）
    ⑧b intraday-exit-gate   週一至五 09:06/09:10/09:12/09:15（1m K 就緒後重試 · 組合閘門 · ON 才寄信）
    ⑧c intraday-open-digest 週一至五 09:06（開盤解讀 · 持倉脈動 · 寄信）
    ⑧d intraday-midday-digest 週一至五 11:00（盤中持倉脈動 · 寄信）
    ②b mutual-fund-watch    週一至五 16:30（ACDD04 月報公布偵測 · 新快照寄信）
    ②a rrg-mono-intraday    週一至五 13:00（收盤前預警 + universe snapshot）
    ②a digest intraday-1300-digest  週一至五 13:02（VCP+RRG+持倉脈動 · 寄信）
    C18acc rrg-c18acc-poll           週一至五 09:00–13:30 每 5 分（swap · dry-run · 不更新槽位）
    Buy  buy-signal-radar            週一至五 09:00–13:20 每 5 分（C0 買進 advisory · 寄信）
    Sell sell-signal-radar           週一至五 09:06–13:20 每 5 分（09:12 前不寄信 · 09:12 起有新訊號才寄）
    SEPA minervini-sepa-basket       週一至五 16:35（月末調倉檢查 · dry-run intent）
    VCP funnel specs          週一至五 13:00（Pivot Gate / Coil Close brief）
    ③ weekly-deep           週日     20:00

  log：${PROJECT_ROOT}/logs/launchd_*.log（收盤／週日等非盤中）
       盤中排程：${PROJECT_ROOT}/logs/intraday/

  注意：Mac 須已登入；睡眠中可能不觸發。
        evening-holdings · rrg-c18acc-poll · vcp-funnel-specs · rrg-mono-intraday · intraday-1300-digest · intraday-midday-digest · intraday-open-digest · mutual-fund · minervini · weekly-deep 以 Application Support launcher + /bin/bash 背景執行（不 open -gj Documents 內 .command）。
        手動除錯仍可用 scripts/launchd/*.command 或 scripts/1630收盤雷達.command。
EOF
}

LAUNCHD_COMMANDS=(
  morning-holdings-brief
  intraday-exit-gate
  evening-holdings
  mutual-fund-disclosure-watch
  rrg-c18acc-poll
  buy-signal-radar
  sell-signal-radar
  rrg-mono-intraday-watch
  intraday-open-digest
  intraday-midday-digest
  intraday-1300-digest
  vcp-funnel-specs
  minervini-sepa-basket
  weekly-deep
)

ensure_launchd_commands() {
  local name path
  for name in "${LAUNCHD_COMMANDS[@]}"; do
    path="${PROJECT_ROOT}/scripts/launchd/${name}.command"
    if [[ ! -f "${path}" ]]; then
      echo "✗ 缺少 ${path}" >&2
      exit 1
    fi
    chmod +x "${path}"
  done
}

verify_documents_launch() {
  local probe="${PROJECT_ROOT}/scripts/launchd/.tcc-probe.command"
  local probe_log="/tmp/com.jackm4.etf.tcc-probe.log"
  cat >"${probe}" <<'PROBE'
#!/bin/bash
echo OK > /tmp/com.jackm4.etf.tcc-probe.log
PROBE
  chmod +x "${probe}"
  rm -f "${probe_log}"

  if ! /usr/bin/open -gj "${probe}" 2>/dev/null; then
    rm -f "${probe}"
    echo "⚠ 無法以 open 觸發探測腳本；請確認 macOS 允許背景啟動 .command" >&2
    return 0
  fi

  local i ok=0
  for i in $(seq 1 15); do
    if [[ -f "${probe_log}" ]]; then
      ok=1
      break
    fi
    sleep 1
  done
  rm -f "${probe}" "${probe_log}"

  if [[ "${ok}" -eq 1 ]]; then
    echo "✓ Documents TCC 探測（open → .command）"
  else
    echo "⚠ TCC 探測逾時；若排程仍失敗，請將 Terminal 加入「完整磁碟取用權限」後重試" >&2
  fi
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

RETIRED_LABELS=(
  com.jackm4.etf.rrg-mono-scan
  com.jackm4.etf.vcp-intraday-watch
  com.jackm4.etf.morning-regime
  com.jackm4.etf.test-doc-bash
  com.jackm4.etf.c18acc-extension-overlay
)

uninstall_retired_agents() {
  local label dest
  for label in "${RETIRED_LABELS[@]}"; do
    bootout_label "${label}"
    dest="${AGENT_DIR}/${label}.plist"
    if [[ -f "${dest}" ]]; then
      rm -f "${dest}"
      echo "✓ 已移除退役 ${label}"
    fi
  done
}

generate_c18acc_calendar_intervals() {
  local out="/tmp/com.jackm4.etf.c18acc-calendar.xml"
  {
    printf '\t<key>StartCalendarInterval</key>\n'
    printf '\t<array>\n'
    local wd hour minute
    for wd in 1 2 3 4 5; do
      for hour in 9 10 11 12 13; do
        for minute in 0 5 10 15 20 25 30 35 40 45 50 55; do
          if [[ "${hour}" -eq 13 && "${minute}" -gt 30 ]]; then
            continue
          fi
          printf '\t\t<dict>\n'
          printf '\t\t\t<key>Weekday</key><integer>%s</integer>\n' "${wd}"
          printf '\t\t\t<key>Hour</key><integer>%s</integer>\n' "${hour}"
          printf '\t\t\t<key>Minute</key><integer>%s</integer>\n' "${minute}"
          printf '\t\t</dict>\n'
        done
      done
    done
    printf '\t</array>\n'
  } >"${out}"
  C18ACC_CALENDAR_INTERVALS_FILE="${out}"
}

generate_buy_radar_calendar_intervals() {
  local out="/tmp/com.jackm4.etf.buy-radar-calendar.xml"
  {
    printf '\t<key>StartCalendarInterval</key>\n'
    printf '\t<array>\n'
    local wd hour minute
    for wd in 1 2 3 4 5; do
      for hour in 9 10 11 12 13; do
        for minute in 0 5 10 15 20 25 30 35 40 45 50 55; do
          if [[ "${hour}" -eq 13 && "${minute}" -gt 20 ]]; then
            continue
          fi
          printf '\t\t<dict>\n'
          printf '\t\t\t<key>Weekday</key><integer>%s</integer>\n' "${wd}"
          printf '\t\t\t<key>Hour</key><integer>%s</integer>\n' "${hour}"
          printf '\t\t\t<key>Minute</key><integer>%s</integer>\n' "${minute}"
          printf '\t\t</dict>\n'
        done
      done
    done
    printf '\t</array>\n'
  } >"${out}"
  BUY_RADAR_CALENDAR_INTERVALS_FILE="${out}"
}

generate_sell_radar_calendar_intervals() {
  local out="/tmp/com.jackm4.etf.sell-radar-calendar.xml"
  {
    printf '\t<key>StartCalendarInterval</key>\n'
    printf '\t<array>\n'
    local wd hour minute
    for wd in 1 2 3 4 5; do
      for hour in 9 10 11 12 13; do
        for minute in 0 5 10 15 20 25 30 35 40 45 50 55; do
          if [[ "${hour}" -eq 9 && "${minute}" -lt 6 ]]; then
            continue
          fi
          if [[ "${hour}" -eq 13 && "${minute}" -gt 20 ]]; then
            continue
          fi
          printf '\t\t<dict>\n'
          printf '\t\t\t<key>Weekday</key><integer>%s</integer>\n' "${wd}"
          printf '\t\t\t<key>Hour</key><integer>%s</integer>\n' "${hour}"
          printf '\t\t\t<key>Minute</key><integer>%s</integer>\n' "${minute}"
          printf '\t\t</dict>\n'
        done
      done
    done
    printf '\t</array>\n'
  } >"${out}"
  SELL_RADAR_CALENDAR_INTERVALS_FILE="${out}"
}

generate_extension_calendar_intervals() {
  local out="/tmp/com.jackm4.etf.extension-calendar.xml"
  {
    printf '\t<key>StartCalendarInterval</key>\n'
    printf '\t<array>\n'
    local wd hour minute
    for wd in 1 2 3 4 5; do
      for hour in 9 10 11 12 13; do
        for minute in $(seq 0 59); do
          if [[ "${hour}" -eq 9 && "${minute}" -lt 6 ]]; then
            continue
          fi
          if [[ "${hour}" -eq 13 && "${minute}" -gt 20 ]]; then
            continue
          fi
          printf '\t\t<dict>\n'
          printf '\t\t\t<key>Weekday</key><integer>%s</integer>\n' "${wd}"
          printf '\t\t\t<key>Hour</key><integer>%s</integer>\n' "${hour}"
          printf '\t\t\t<key>Minute</key><integer>%s</integer>\n' "${minute}"
          printf '\t\t</dict>\n'
        done
      done
    done
    printf '\t</array>\n'
  } >"${out}"
  EXTENSION_CALENDAR_INTERVALS_FILE="${out}"
}

render_template() {
  local template="$1"
  local dest="$2"
  if grep -q '{{BUY_RADAR_CALENDAR_INTERVALS}}' "${template}"; then
    generate_buy_radar_calendar_intervals
    sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
        -e "s|{{C18ACC_LAUNCHER}}|${C18ACC_LAUNCHER}|g" \
        -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
        -e "s|{{SELL_RADAR_LAUNCHER}}|${SELL_RADAR_LAUNCHER}|g" \
        -e "s|{{EXTENSION_LAUNCHER}}|${EXTENSION_LAUNCHER}|g" \
        -e "s|{{EVENING_HOLDINGS_LAUNCHER}}|${EVENING_HOLDINGS_LAUNCHER}|g" \
        -e "s|{{MORNING_BRIEF_LAUNCHER}}|${MORNING_BRIEF_LAUNCHER}|g" \
        -e "s|{{INTRADAY_GATE_LAUNCHER}}|${INTRADAY_GATE_LAUNCHER}|g" \
        -e "s|{{VCP_FUNNEL_LAUNCHER}}|${VCP_FUNNEL_LAUNCHER}|g" \
        -e "s|{{RRG_MONO_INTRADAY_LAUNCHER}}|${RRG_MONO_INTRADAY_LAUNCHER}|g" \
        -e "s|{{INTRADAY_1300_DIGEST_LAUNCHER}}|${INTRADAY_1300_DIGEST_LAUNCHER}|g" \
        -e "s|{{INTRADAY_OPEN_DIGEST_LAUNCHER}}|${INTRADAY_OPEN_DIGEST_LAUNCHER}|g" \
        -e "s|{{INTRADAY_MIDDAY_DIGEST_LAUNCHER}}|${INTRADAY_MIDDAY_DIGEST_LAUNCHER}|g" \
        -e "s|{{MUTUAL_FUND_LAUNCHER}}|${MUTUAL_FUND_LAUNCHER}|g" \
        -e "s|{{MINERVINI_LAUNCHER}}|${MINERVINI_LAUNCHER}|g" \
        -e "s|{{WEEKLY_DEEP_LAUNCHER}}|${WEEKLY_DEEP_LAUNCHER}|g" \
        "${template}" \
      | sed "/{{BUY_RADAR_CALENDAR_INTERVALS}}/r ${BUY_RADAR_CALENDAR_INTERVALS_FILE}" \
      | sed '/{{BUY_RADAR_CALENDAR_INTERVALS}}/d' \
      >"${dest}"
    return
  fi
  if grep -q '{{SELL_RADAR_CALENDAR_INTERVALS}}' "${template}"; then
    generate_sell_radar_calendar_intervals
    sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
        -e "s|{{C18ACC_LAUNCHER}}|${C18ACC_LAUNCHER}|g" \
        -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
        -e "s|{{SELL_RADAR_LAUNCHER}}|${SELL_RADAR_LAUNCHER}|g" \
        -e "s|{{EXTENSION_LAUNCHER}}|${EXTENSION_LAUNCHER}|g" \
        -e "s|{{EVENING_HOLDINGS_LAUNCHER}}|${EVENING_HOLDINGS_LAUNCHER}|g" \
        -e "s|{{MORNING_BRIEF_LAUNCHER}}|${MORNING_BRIEF_LAUNCHER}|g" \
        -e "s|{{INTRADAY_GATE_LAUNCHER}}|${INTRADAY_GATE_LAUNCHER}|g" \
        -e "s|{{VCP_FUNNEL_LAUNCHER}}|${VCP_FUNNEL_LAUNCHER}|g" \
        -e "s|{{RRG_MONO_INTRADAY_LAUNCHER}}|${RRG_MONO_INTRADAY_LAUNCHER}|g" \
        -e "s|{{INTRADAY_1300_DIGEST_LAUNCHER}}|${INTRADAY_1300_DIGEST_LAUNCHER}|g" \
        -e "s|{{INTRADAY_OPEN_DIGEST_LAUNCHER}}|${INTRADAY_OPEN_DIGEST_LAUNCHER}|g" \
        -e "s|{{INTRADAY_MIDDAY_DIGEST_LAUNCHER}}|${INTRADAY_MIDDAY_DIGEST_LAUNCHER}|g" \
        -e "s|{{MUTUAL_FUND_LAUNCHER}}|${MUTUAL_FUND_LAUNCHER}|g" \
        -e "s|{{MINERVINI_LAUNCHER}}|${MINERVINI_LAUNCHER}|g" \
        -e "s|{{WEEKLY_DEEP_LAUNCHER}}|${WEEKLY_DEEP_LAUNCHER}|g" \
        "${template}" \
      | sed "/{{SELL_RADAR_CALENDAR_INTERVALS}}/r ${SELL_RADAR_CALENDAR_INTERVALS_FILE}" \
      | sed '/{{SELL_RADAR_CALENDAR_INTERVALS}}/d' \
      >"${dest}"
    return
  fi
  if grep -q '{{C18ACC_CALENDAR_INTERVALS}}' "${template}"; then
    generate_c18acc_calendar_intervals
    sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
        -e "s|{{C18ACC_LAUNCHER}}|${C18ACC_LAUNCHER}|g" \
        -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
        -e "s|{{SELL_RADAR_LAUNCHER}}|${SELL_RADAR_LAUNCHER}|g" \
        -e "s|{{EXTENSION_LAUNCHER}}|${EXTENSION_LAUNCHER}|g" \
        -e "s|{{EVENING_HOLDINGS_LAUNCHER}}|${EVENING_HOLDINGS_LAUNCHER}|g" \
        -e "s|{{MORNING_BRIEF_LAUNCHER}}|${MORNING_BRIEF_LAUNCHER}|g" \
        -e "s|{{INTRADAY_GATE_LAUNCHER}}|${INTRADAY_GATE_LAUNCHER}|g" \
        -e "s|{{VCP_FUNNEL_LAUNCHER}}|${VCP_FUNNEL_LAUNCHER}|g" \
        -e "s|{{RRG_MONO_INTRADAY_LAUNCHER}}|${RRG_MONO_INTRADAY_LAUNCHER}|g" \
        -e "s|{{INTRADAY_1300_DIGEST_LAUNCHER}}|${INTRADAY_1300_DIGEST_LAUNCHER}|g" \
        -e "s|{{INTRADAY_OPEN_DIGEST_LAUNCHER}}|${INTRADAY_OPEN_DIGEST_LAUNCHER}|g" \
        -e "s|{{INTRADAY_MIDDAY_DIGEST_LAUNCHER}}|${INTRADAY_MIDDAY_DIGEST_LAUNCHER}|g" \
        -e "s|{{MUTUAL_FUND_LAUNCHER}}|${MUTUAL_FUND_LAUNCHER}|g" \
        -e "s|{{MINERVINI_LAUNCHER}}|${MINERVINI_LAUNCHER}|g" \
        -e "s|{{WEEKLY_DEEP_LAUNCHER}}|${WEEKLY_DEEP_LAUNCHER}|g" \
        "${template}" \
      | sed "/{{C18ACC_CALENDAR_INTERVALS}}/r ${C18ACC_CALENDAR_INTERVALS_FILE}" \
      | sed '/{{C18ACC_CALENDAR_INTERVALS}}/d' \
      >"${dest}"
    return
  fi
  if grep -q '{{EXTENSION_CALENDAR_INTERVALS}}' "${template}"; then
    generate_extension_calendar_intervals
    sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
        -e "s|{{C18ACC_LAUNCHER}}|${C18ACC_LAUNCHER}|g" \
        -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
        -e "s|{{SELL_RADAR_LAUNCHER}}|${SELL_RADAR_LAUNCHER}|g" \
        -e "s|{{EXTENSION_LAUNCHER}}|${EXTENSION_LAUNCHER}|g" \
        -e "s|{{EVENING_HOLDINGS_LAUNCHER}}|${EVENING_HOLDINGS_LAUNCHER}|g" \
        -e "s|{{MORNING_BRIEF_LAUNCHER}}|${MORNING_BRIEF_LAUNCHER}|g" \
        -e "s|{{INTRADAY_GATE_LAUNCHER}}|${INTRADAY_GATE_LAUNCHER}|g" \
        -e "s|{{VCP_FUNNEL_LAUNCHER}}|${VCP_FUNNEL_LAUNCHER}|g" \
        -e "s|{{RRG_MONO_INTRADAY_LAUNCHER}}|${RRG_MONO_INTRADAY_LAUNCHER}|g" \
        -e "s|{{INTRADAY_1300_DIGEST_LAUNCHER}}|${INTRADAY_1300_DIGEST_LAUNCHER}|g" \
        -e "s|{{INTRADAY_OPEN_DIGEST_LAUNCHER}}|${INTRADAY_OPEN_DIGEST_LAUNCHER}|g" \
        -e "s|{{INTRADAY_MIDDAY_DIGEST_LAUNCHER}}|${INTRADAY_MIDDAY_DIGEST_LAUNCHER}|g" \
        -e "s|{{MUTUAL_FUND_LAUNCHER}}|${MUTUAL_FUND_LAUNCHER}|g" \
        -e "s|{{MINERVINI_LAUNCHER}}|${MINERVINI_LAUNCHER}|g" \
        -e "s|{{WEEKLY_DEEP_LAUNCHER}}|${WEEKLY_DEEP_LAUNCHER}|g" \
        "${template}" \
      | sed "/{{EXTENSION_CALENDAR_INTERVALS}}/r ${EXTENSION_CALENDAR_INTERVALS_FILE}" \
      | sed '/{{EXTENSION_CALENDAR_INTERVALS}}/d' \
      >"${dest}"
    return
  fi
  sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
      -e "s|{{C18ACC_LAUNCHER}}|${C18ACC_LAUNCHER}|g" \
      -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
      -e "s|{{SELL_RADAR_LAUNCHER}}|${SELL_RADAR_LAUNCHER}|g" \
      -e "s|{{EXTENSION_LAUNCHER}}|${EXTENSION_LAUNCHER}|g" \
      -e "s|{{EVENING_HOLDINGS_LAUNCHER}}|${EVENING_HOLDINGS_LAUNCHER}|g" \
      -e "s|{{MORNING_BRIEF_LAUNCHER}}|${MORNING_BRIEF_LAUNCHER}|g" \
      -e "s|{{INTRADAY_GATE_LAUNCHER}}|${INTRADAY_GATE_LAUNCHER}|g" \
      -e "s|{{VCP_FUNNEL_LAUNCHER}}|${VCP_FUNNEL_LAUNCHER}|g" \
      -e "s|{{RRG_MONO_INTRADAY_LAUNCHER}}|${RRG_MONO_INTRADAY_LAUNCHER}|g" \
      -e "s|{{INTRADAY_1300_DIGEST_LAUNCHER}}|${INTRADAY_1300_DIGEST_LAUNCHER}|g" \
      -e "s|{{INTRADAY_OPEN_DIGEST_LAUNCHER}}|${INTRADAY_OPEN_DIGEST_LAUNCHER}|g" \
      -e "s|{{INTRADAY_MIDDAY_DIGEST_LAUNCHER}}|${INTRADAY_MIDDAY_DIGEST_LAUNCHER}|g" \
      -e "s|{{MUTUAL_FUND_LAUNCHER}}|${MUTUAL_FUND_LAUNCHER}|g" \
      -e "s|{{MINERVINI_LAUNCHER}}|${MINERVINI_LAUNCHER}|g" \
      -e "s|{{WEEKLY_DEEP_LAUNCHER}}|${WEEKLY_DEEP_LAUNCHER}|g" \
      "${template}" >"${dest}"
}

install_c18acc_launcher() {
  local src="${LAUNCHD_SRC}/rrg-c18acc-poll-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  C18ACC_LAUNCHER="${app_support}/rrg-c18acc-poll.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${C18ACC_LAUNCHER}"
  chmod +x "${C18ACC_LAUNCHER}"
}

install_extension_launcher() {
  local src="${LAUNCHD_SRC}/c18acc-extension-overlay-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  EXTENSION_LAUNCHER="${app_support}/c18acc-extension-overlay.sh"
  if [[ ! -f "${src}" ]]; then
    EXTENSION_LAUNCHER=""
    return 0
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${EXTENSION_LAUNCHER}"
  chmod +x "${EXTENSION_LAUNCHER}"
}

install_buy_radar_launcher() {
  local src="${LAUNCHD_SRC}/buy-signal-radar-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  BUY_RADAR_LAUNCHER="${app_support}/buy-signal-radar.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${BUY_RADAR_LAUNCHER}"
  chmod +x "${BUY_RADAR_LAUNCHER}"
}

install_sell_radar_launcher() {
  local src="${LAUNCHD_SRC}/sell-signal-radar-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  SELL_RADAR_LAUNCHER="${app_support}/sell-signal-radar.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${SELL_RADAR_LAUNCHER}"
  chmod +x "${SELL_RADAR_LAUNCHER}"
}

install_evening_holdings_launcher() {
  local src="${LAUNCHD_SRC}/evening-holdings-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  EVENING_HOLDINGS_LAUNCHER="${app_support}/evening-holdings.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${EVENING_HOLDINGS_LAUNCHER}"
  chmod +x "${EVENING_HOLDINGS_LAUNCHER}"
}

install_morning_brief_launcher() {
  local src="${LAUNCHD_SRC}/morning-holdings-brief-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  MORNING_BRIEF_LAUNCHER="${app_support}/morning-holdings-brief.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${MORNING_BRIEF_LAUNCHER}"
  chmod +x "${MORNING_BRIEF_LAUNCHER}"
}

install_intraday_gate_launcher() {
  local src="${LAUNCHD_SRC}/intraday-exit-gate-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  INTRADAY_GATE_LAUNCHER="${app_support}/intraday-exit-gate.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${INTRADAY_GATE_LAUNCHER}"
  chmod +x "${INTRADAY_GATE_LAUNCHER}"
}

install_vcp_funnel_launcher() {
  local src="${LAUNCHD_SRC}/vcp-funnel-specs-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  VCP_FUNNEL_LAUNCHER="${app_support}/vcp-funnel-specs.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${VCP_FUNNEL_LAUNCHER}"
  chmod +x "${VCP_FUNNEL_LAUNCHER}"
}

install_rrg_mono_intraday_launcher() {
  local src="${LAUNCHD_SRC}/rrg-mono-intraday-watch-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  RRG_MONO_INTRADAY_LAUNCHER="${app_support}/rrg-mono-intraday-watch.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${RRG_MONO_INTRADAY_LAUNCHER}"
  chmod +x "${RRG_MONO_INTRADAY_LAUNCHER}"
}

install_intraday_1300_digest_launcher() {
  local src="${LAUNCHD_SRC}/intraday-1300-digest-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  INTRADAY_1300_DIGEST_LAUNCHER="${app_support}/intraday-1300-digest.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${INTRADAY_1300_DIGEST_LAUNCHER}"
  chmod +x "${INTRADAY_1300_DIGEST_LAUNCHER}"
}

install_intraday_open_digest_launcher() {
  local src="${LAUNCHD_SRC}/intraday-open-digest-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  INTRADAY_OPEN_DIGEST_LAUNCHER="${app_support}/intraday-open-digest.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${INTRADAY_OPEN_DIGEST_LAUNCHER}"
  chmod +x "${INTRADAY_OPEN_DIGEST_LAUNCHER}"
}

install_intraday_midday_digest_launcher() {
  local src="${LAUNCHD_SRC}/intraday-midday-digest-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  INTRADAY_MIDDAY_DIGEST_LAUNCHER="${app_support}/intraday-midday-digest.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${INTRADAY_MIDDAY_DIGEST_LAUNCHER}"
  chmod +x "${INTRADAY_MIDDAY_DIGEST_LAUNCHER}"
}

install_mutual_fund_launcher() {
  local src="${LAUNCHD_SRC}/mutual-fund-disclosure-watch-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  MUTUAL_FUND_LAUNCHER="${app_support}/mutual-fund-disclosure-watch.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${MUTUAL_FUND_LAUNCHER}"
  chmod +x "${MUTUAL_FUND_LAUNCHER}"
}

install_minervini_launcher() {
  local src="${LAUNCHD_SRC}/minervini-sepa-basket-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  MINERVINI_LAUNCHER="${app_support}/minervini-sepa-basket.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${MINERVINI_LAUNCHER}"
  chmod +x "${MINERVINI_LAUNCHER}"
}

install_weekly_deep_launcher() {
  local src="${LAUNCHD_SRC}/weekly-deep-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  WEEKLY_DEEP_LAUNCHER="${app_support}/weekly-deep.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${WEEKLY_DEEP_LAUNCHER}"
  chmod +x "${WEEKLY_DEEP_LAUNCHER}"
}

migrate_intraday_logs() {
  local src_dir="${PROJECT_ROOT}/logs"
  local dst_dir="${src_dir}/intraday"
  mkdir -p "${dst_dir}"
  local f base moved=0
  shopt -s nullglob
  for f in \
    "${src_dir}"/launchd_buy-signal-radar* \
    "${src_dir}"/launchd_sell-signal-radar* \
    "${src_dir}"/launchd_rrg-c18acc-poll* \
    "${src_dir}"/launchd_intraday-* \
    "${src_dir}"/launchd_rrg-mono-intraday-watch* \
    "${src_dir}"/launchd_vcp-funnel-specs* \
    "${src_dir}"/launchd_morning-holdings-brief* \
    "${src_dir}"/launchd_c18acc-extension-overlay* \
    "${src_dir}"/buy_signal_radar_*.log \
    "${src_dir}"/sell_signal_radar_*.log \
    "${src_dir}"/rrg_c18acc_poll_tick.log \
    "${src_dir}"/c18acc_extension_poll_tick.log \
    "${src_dir}"/intraday_exit_gate_*.log \
    "${src_dir}"/intraday_open_digest_*.log \
    "${src_dir}"/intraday_midday_digest_*.log \
    "${src_dir}"/intraday_1300_digest_*.log \
    "${src_dir}"/morning_holdings_brief_*.log
  do
    [[ -f "${f}" ]] || continue
    base="$(basename "${f}")"
    if [[ -e "${dst_dir}/${base}" ]]; then
      continue
    fi
    mv "${f}" "${dst_dir}/"
    moved=$((moved + 1))
    echo "  搬移 logs/${base} → logs/intraday/"
  done
  shopt -u nullglob
  if [[ "${moved}" -gt 0 ]]; then
    echo "✓ 盤中 log 已整理至 logs/intraday/（${moved} 檔）"
  fi
}

install_agents() {
  if [[ ! -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
    echo "✗ 找不到 ${PROJECT_ROOT}/.venv/bin/python" >&2
    echo "  請先在專案根目錄建立 venv 並安裝 requirements.txt" >&2
    exit 1
  fi

  mkdir -p "${AGENT_DIR}" "${PROJECT_ROOT}/logs" "${PROJECT_ROOT}/logs/intraday"
  migrate_intraday_logs
  uninstall_retired_agents
  ensure_launchd_commands
  C18ACC_LAUNCHER=""
  EXTENSION_LAUNCHER=""
  BUY_RADAR_LAUNCHER=""
  SELL_RADAR_LAUNCHER=""
  EVENING_HOLDINGS_LAUNCHER=""
  MORNING_BRIEF_LAUNCHER=""
  INTRADAY_GATE_LAUNCHER=""
  VCP_FUNNEL_LAUNCHER=""
  RRG_MONO_INTRADAY_LAUNCHER=""
  INTRADAY_1300_DIGEST_LAUNCHER=""
  INTRADAY_OPEN_DIGEST_LAUNCHER=""
  INTRADAY_MIDDAY_DIGEST_LAUNCHER=""
  MUTUAL_FUND_LAUNCHER=""
  MINERVINI_LAUNCHER=""
  WEEKLY_DEEP_LAUNCHER=""
  install_c18acc_launcher
  install_extension_launcher
  install_buy_radar_launcher
  install_sell_radar_launcher
  install_evening_holdings_launcher
  install_morning_brief_launcher
  install_intraday_gate_launcher
  install_vcp_funnel_launcher
  install_rrg_mono_intraday_launcher
  install_intraday_1300_digest_launcher
  install_intraday_open_digest_launcher
  install_intraday_midday_digest_launcher
  install_mutual_fund_launcher
  install_minervini_launcher
  install_weekly_deep_launcher

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
  verify_documents_launch
  echo ""
  echo "完成。檢查："
  echo "  launchctl list | grep jackm4.etf"
  echo "  tail -f ${PROJECT_ROOT}/logs/intraday/launchd_buy-signal-radar.log"
  echo "  tail -f ${PROJECT_ROOT}/logs/launchd_evening-holdings.log"
  echo "  tail -f ${PROJECT_ROOT}/logs/daily_sync_\$(date +%Y%m%d).log"
}

uninstall_agents() {
  local label dest
  uninstall_retired_agents
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
