#!/usr/bin/env bash
# 安裝下單層 launchd（C18acc · Leading Dip · sell/exit ops）
# ABC Order 已退役（2026-07-15）。Facts / Regime / VCP / digest 等已退役排程（可手動跑）。
#
# 用法：
#   scripts/install-launchd.sh           # 安裝並載入
#   scripts/install-launchd.sh --uninstall
#   scripts/install-launchd.sh --status

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCHD_SRC="${PROJECT_ROOT}/launchd"
APP_SUPPORT="${HOME}/Library/Application Support/com.jackm4.etf"
AGENT_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"

# 研究層 collect job（fubon-premarket/fubon-intraday-quote-collect · pre-market-auction-collect）刻意不在此陣列：plist 佔位符無 launcher template，安裝方式見各 scripts/launchd/*collect*.command 檔頭（一次性手動 sed + bootstrap）。
LABELS=(
  com.jackm4.etf.rrg-c18acc-poll
  com.jackm4.etf.buy-signal-radar
  com.jackm4.etf.sell-signal-radar
  com.jackm4.etf.detach-gate
  com.jackm4.etf.leading-dip-poll
  com.jackm4.etf.songshan-copytrade-poll
  com.jackm4.etf.timed-limit-orders
  com.jackm4.etf.expert-pool-staged-gate
  com.jackm4.etf.winbond-expert-pool-watch
  com.jackm4.etf.second-disp-expert-pool-watch
  com.jackm4.etf.expert-pool-chart-digest
  com.jackm4.etf.holdings-branch-sell-monitor
  com.jackm4.etf.branch-tape-prewarm
  com.jackm4.etf.crash-thermometer-daily
  com.jackm4.etf.ops-live-ta-poll
  com.jackm4.etf.ops-console-evening-sync
)

TEMPLATES=(
  com.jackm4.etf.rrg-c18acc-poll.plist.template
  com.jackm4.etf.buy-signal-radar.plist.template
  com.jackm4.etf.sell-signal-radar.plist.template
  com.jackm4.etf.detach-gate.plist.template
  com.jackm4.etf.leading-dip-poll.plist.template
  com.jackm4.etf.songshan-copytrade-poll.plist.template
  com.jackm4.etf.timed-limit-orders.plist.template
  com.jackm4.etf.expert-pool-staged-gate.plist.template
  com.jackm4.etf.winbond-expert-pool-watch.plist.template
  com.jackm4.etf.second-disp-expert-pool-watch.plist.template
  com.jackm4.etf.expert-pool-chart-digest.plist.template
  com.jackm4.etf.holdings-branch-sell-monitor.plist.template
  com.jackm4.etf.branch-tape-prewarm.plist.template
  com.jackm4.etf.crash-thermometer-daily.plist.template
  com.jackm4.etf.ops-live-ta-poll.plist.template
  com.jackm4.etf.ops-console-evening-sync.plist.template
)

usage() {
  cat <<EOF
用法: $(basename "$0") [--uninstall|--status]

  預設：將 launchd/*.plist.template 渲染後安裝到
        ~/Library/LaunchAgents/ 並 launchctl load。

  現行排程（Order layer · C18acc / Leading Dip / Songshan；本地時間）：
    rrg-c18acc-poll         週一至五 09:00–13:30 每 5 分（C18acc swap · auto-submit）
    buy-signal-radar        週一至五 09:00–13:20 每 5 分（notify；ABC Order 已退役）
    sell-signal-radar       週一至五 09:06–13:20 每 5 分（extension 持倉賣出 advisory）
    detach-gate             週一至五 09:40–12:30 每 5 分（台美脫鉤閘門 · 半倉買一）
    leading-dip-poll        週一至五 09:05–13:25 每 5 分（Leading Dip · 獨立袖套 · 預設 dry-run）
    songshan-copytrade-poll 週一至五 09:25–09:40 每 5 分（跟單松山 5d淨比95∩!mega+25m nonfail · 1 張）
    timed-limit-orders      週一至五 09:05（config timed_limit_orders · 逾時撤；6451 once_date）
    expert-pool-staged-gate 週一至五 09:00／01／05／25（專家池 gap→05→25 漏斗閘門 · 預設 dry-run）
    winbond-expert-pool-watch  週一至五 20:00（專家池+松山+新店 輕量 digest · 不下單）
    second-disp-expert-pool-watch  週一至五 20:35（處置股專家池跟單 · T0濾網 · 不下單）
    expert-pool-chart-digest   週一至五 20:05（專家池達標 HTML 圖文 · 無達標不寄 · 不下單）
    holdings-branch-sell-monitor  週一至五 20:10（富邦持倉×專家分點淨賣預警 · 不下單）
    branch-tape-prewarm        週一至五 18:30（分點 tape 補檔 POOLS∪持倉 · 讓 20:00 起純讀 DB · 不下單）
    crash-thermometer-daily    週一至五 09:00（大跌溫度計 · 加權8家+共識 · email · 不下單）
    ops-live-ta-poll           週一至五 08:50–13:35 每 15 秒（持倉 Live TA → ops.live_ta · 不下單）
    ops-console-evening-sync   週一至五 20:40（ops.snapshots／sleeve／holdings 上牆 · 不下單）

  已退役（不再安裝；手動仍可用 scripts/launchd/*.command 或 1630收盤雷達）：
    specialty-expert-pool-watch（已併入 winbond-expert-pool-watch 統一入口）·
    morning-holdings-brief（2026-07-16 退役 · 手動仍可用 scripts/order/morning_holdings_brief.py）·
    ABC v3+f1 Order（buy-radar 不再送單）·
    intraday-exit-gate（結構停損閘門 · 已退回 Research）·
    evening-holdings · digests · vcp-funnel-specs · minervini-sepa-basket ·
    mutual-fund-disclosure-watch · rrg-mono-intraday-watch · weekly-deep ·
    c18acc-extension-overlay

  log：盤中 ${PROJECT_ROOT}/logs/intraday/

  注意：Mac 須已登入。盤中 poll 用 StartInterval=300（非 Aqua CalendarInterval），
        開盤窗由 launcher 過濾；order-wake 每 5 分 caffeinate。launchd stdout 寫
        ~/Library/Logs/com.jackm4.etf/（避開 Documents TCC → EX_CONFIG）。
        rrg-c18acc-poll · leading-dip-poll · buy/sell radar · detach-gate
        以 Application Support launcher + /bin/bash 背景執行。
EOF
}

LAUNCHD_COMMANDS=(
  rrg-c18acc-poll
  buy-signal-radar
  sell-signal-radar
  detach-gate
  leading-dip-poll
  songshan-copytrade-poll
  timed-limit-orders
  expert-pool-staged-gate
  winbond-expert-pool-watch
  second-disp-expert-pool-watch
  expert-pool-chart-digest
  holdings-branch-sell-monitor
  branch-tape-prewarm
  crash-thermometer-daily
  ops-live-ta-poll
  ops-console-evening-sync
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
  com.jackm4.etf.morning-holdings-brief
  com.jackm4.etf.rrg-mono-scan
  com.jackm4.etf.vcp-intraday-watch
  com.jackm4.etf.morning-regime
  com.jackm4.etf.test-doc-bash
  com.jackm4.etf.c18acc-extension-overlay
  com.jackm4.etf.weekly-deep
  com.jackm4.etf.rrg-mono-intraday-watch
  com.jackm4.etf.evening-holdings
  com.jackm4.etf.mutual-fund-disclosure-watch
  com.jackm4.etf.intraday-open-digest
  com.jackm4.etf.intraday-midday-digest
  com.jackm4.etf.intraday-1300-digest
  com.jackm4.etf.vcp-funnel-specs
  com.jackm4.etf.minervini-sepa-basket
  com.jackm4.etf.intraday-exit-gate
  com.jackm4.etf.specialty-expert-pool-watch
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

# macOS UserEventAgent-Aqua 的 StartCalendarInterval 在螢幕休眠／閒置後會整排停火
# （2026-07-16 mini：09:41 後全 Order poll 全滅；改 Minute-only 仍不穩）。
# 改用 launchd 原生 StartInterval=300；開盤窗由各 launcher 腳本過濾。
generate_five_minute_clock_calendar() {
  local out="$1"
  {
    printf '\t<key>StartInterval</key>\n'
    printf '\t<integer>300</integer>\n'
  } >"${out}"
}

generate_c18acc_calendar_intervals() {
  local out="/tmp/com.jackm4.etf.c18acc-calendar.xml"
  generate_five_minute_clock_calendar "${out}"
  C18ACC_CALENDAR_INTERVALS_FILE="${out}"
}

generate_buy_radar_calendar_intervals() {
  local out="/tmp/com.jackm4.etf.buy-radar-calendar.xml"
  generate_five_minute_clock_calendar "${out}"
  BUY_RADAR_CALENDAR_INTERVALS_FILE="${out}"
}

generate_sell_radar_calendar_intervals() {
  local out="/tmp/com.jackm4.etf.sell-radar-calendar.xml"
  generate_five_minute_clock_calendar "${out}"
  SELL_RADAR_CALENDAR_INTERVALS_FILE="${out}"
}

generate_detach_gate_calendar_intervals() {
  local out="/tmp/com.jackm4.etf.detach-gate-calendar.xml"
  generate_five_minute_clock_calendar "${out}"
  DETACH_GATE_CALENDAR_INTERVALS_FILE="${out}"
}

generate_leading_dip_calendar_intervals() {
  local out="/tmp/com.jackm4.etf.leading-dip-calendar.xml"
  generate_five_minute_clock_calendar "${out}"
  LEADING_DIP_CALENDAR_INTERVALS_FILE="${out}"
}

generate_songshan_copytrade_calendar_intervals() {
  local out="/tmp/com.jackm4.etf.songshan-copytrade-calendar.xml"
  generate_five_minute_clock_calendar "${out}"
  SONGSHAN_COPYTRADE_CALENDAR_INTERVALS_FILE="${out}"
}

generate_extension_calendar_intervals() {
  local out="/tmp/com.jackm4.etf.extension-calendar.xml"
  generate_five_minute_clock_calendar "${out}"
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
        -e "s|{{DETACH_GATE_LAUNCHER}}|${DETACH_GATE_LAUNCHER}|g" \
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
        -e "s|{{DETACH_GATE_LAUNCHER}}|${DETACH_GATE_LAUNCHER}|g" \
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
  if grep -q '{{DETACH_GATE_CALENDAR_INTERVALS}}' "${template}"; then
    generate_detach_gate_calendar_intervals
    sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
        -e "s|{{APP_SUPPORT}}|${APP_SUPPORT:-${HOME}/Library/Application Support/com.jackm4.etf}|g" \
        -e "s|{{C18ACC_LAUNCHER}}|${C18ACC_LAUNCHER}|g" \
        -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
        -e "s|{{SELL_RADAR_LAUNCHER}}|${SELL_RADAR_LAUNCHER}|g" \
        -e "s|{{DETACH_GATE_LAUNCHER}}|${DETACH_GATE_LAUNCHER}|g" \
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
      | sed "/{{DETACH_GATE_CALENDAR_INTERVALS}}/r ${DETACH_GATE_CALENDAR_INTERVALS_FILE}" \
      | sed '/{{DETACH_GATE_CALENDAR_INTERVALS}}/d' \
      >"${dest}"
    return
  fi
  if grep -q '{{LEADING_DIP_CALENDAR_INTERVALS}}' "${template}"; then
    generate_leading_dip_calendar_intervals
    sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
        -e "s|{{APP_SUPPORT}}|${APP_SUPPORT:-${HOME}/Library/Application Support/com.jackm4.etf}|g" \
        -e "s|{{C18ACC_LAUNCHER}}|${C18ACC_LAUNCHER}|g" \
        -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
        -e "s|{{SELL_RADAR_LAUNCHER}}|${SELL_RADAR_LAUNCHER}|g" \
        -e "s|{{DETACH_GATE_LAUNCHER}}|${DETACH_GATE_LAUNCHER}|g" \
        -e "s|{{LEADING_DIP_LAUNCHER}}|${LEADING_DIP_LAUNCHER}|g" \
        -e "s|{{SONGSHAN_COPYTRADE_LAUNCHER}}|${SONGSHAN_COPYTRADE_LAUNCHER}|g" \
        -e "s|{{TIMED_LIMIT_LAUNCHER}}|${TIMED_LIMIT_LAUNCHER}|g" \
        -e "s|{{EP_STAGED_GATE_LAUNCHER}}|${EP_STAGED_GATE_LAUNCHER}|g" \
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
      | sed "/{{LEADING_DIP_CALENDAR_INTERVALS}}/r ${LEADING_DIP_CALENDAR_INTERVALS_FILE}" \
      | sed '/{{LEADING_DIP_CALENDAR_INTERVALS}}/d' \
      >"${dest}"
    return
  fi
  if grep -q '{{SONGSHAN_COPYTRADE_CALENDAR_INTERVALS}}' "${template}"; then
    generate_songshan_copytrade_calendar_intervals
    sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
        -e "s|{{APP_SUPPORT}}|${APP_SUPPORT:-${HOME}/Library/Application Support/com.jackm4.etf}|g" \
        -e "s|{{SONGSHAN_COPYTRADE_LAUNCHER}}|${SONGSHAN_COPYTRADE_LAUNCHER}|g" \
        -e "s|{{TIMED_LIMIT_LAUNCHER}}|${TIMED_LIMIT_LAUNCHER}|g" \
        -e "s|{{EP_STAGED_GATE_LAUNCHER}}|${EP_STAGED_GATE_LAUNCHER}|g" \
        -e "s|{{LEADING_DIP_LAUNCHER}}|${LEADING_DIP_LAUNCHER}|g" \
        "${template}" \
      | sed "/{{SONGSHAN_COPYTRADE_CALENDAR_INTERVALS}}/r ${SONGSHAN_COPYTRADE_CALENDAR_INTERVALS_FILE}" \
      | sed '/{{SONGSHAN_COPYTRADE_CALENDAR_INTERVALS}}/d' \
      >"${dest}"
    return
  fi
  if grep -q '{{C18ACC_CALENDAR_INTERVALS}}' "${template}"; then
    generate_c18acc_calendar_intervals
    sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
        -e "s|{{C18ACC_LAUNCHER}}|${C18ACC_LAUNCHER}|g" \
        -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
        -e "s|{{SELL_RADAR_LAUNCHER}}|${SELL_RADAR_LAUNCHER}|g" \
        -e "s|{{DETACH_GATE_LAUNCHER}}|${DETACH_GATE_LAUNCHER}|g" \
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
        -e "s|{{DETACH_GATE_LAUNCHER}}|${DETACH_GATE_LAUNCHER}|g" \
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
      -e "s|{{APP_SUPPORT}}|${APP_SUPPORT:-${HOME}/Library/Application Support/com.jackm4.etf}|g" \
      -e "s|{{C18ACC_LAUNCHER}}|${C18ACC_LAUNCHER}|g" \
      -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
      -e "s|{{SELL_RADAR_LAUNCHER}}|${SELL_RADAR_LAUNCHER}|g" \
      -e "s|{{DETACH_GATE_LAUNCHER}}|${DETACH_GATE_LAUNCHER}|g" \
      -e "s|{{LEADING_DIP_LAUNCHER}}|${LEADING_DIP_LAUNCHER}|g" \
      -e "s|{{SONGSHAN_COPYTRADE_LAUNCHER}}|${SONGSHAN_COPYTRADE_LAUNCHER}|g" \
      -e "s|{{TIMED_LIMIT_LAUNCHER}}|${TIMED_LIMIT_LAUNCHER}|g" \
      -e "s|{{EP_STAGED_GATE_LAUNCHER}}|${EP_STAGED_GATE_LAUNCHER}|g" \
      -e "s|{{WINBOND_EXPERT_LAUNCHER}}|${WINBOND_EXPERT_LAUNCHER}|g" \
      -e "s|{{SECOND_DISP_EXPERT_LAUNCHER}}|${SECOND_DISP_EXPERT_LAUNCHER}|g" \
      -e "s|{{EXPERT_POOL_CHART_LAUNCHER}}|${EXPERT_POOL_CHART_LAUNCHER}|g" \
      -e "s|{{HOLDINGS_BRANCH_SELL_LAUNCHER}}|${HOLDINGS_BRANCH_SELL_LAUNCHER}|g" \
      -e "s|{{BRANCH_TAPE_PREWARM_LAUNCHER}}|${BRANCH_TAPE_PREWARM_LAUNCHER}|g" \
      -e "s|{{CRASH_THERMOMETER_LAUNCHER}}|${CRASH_THERMOMETER_LAUNCHER}|g" \
      -e "s|{{OPS_LIVE_TA_LAUNCHER}}|${OPS_LIVE_TA_LAUNCHER}|g" \
      -e "s|{{OPS_CONSOLE_EVENING_LAUNCHER}}|${OPS_CONSOLE_EVENING_LAUNCHER}|g" \
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

sync_order_env_mirror() {
  # Mirror non-secret ORDER_/C18ACC_ keys for launchd (Documents .env may be TCC-blocked).
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  local src_env="${PROJECT_ROOT}/.env"
  local dest="${app_support}/order.env"
  mkdir -p "${app_support}"
  {
    echo "# Generated by install-launchd.sh · $(date '+%Y-%m-%d %H:%M:%S')"
    echo "# Whitelist only · no passwords / cert paths"
    if [[ -f "${src_env}" ]]; then
      # shellcheck disable=SC2016
      grep -E '^(ORDER_C18ACC_|C18ACC_|ORDER_RESERVED_CASH_|ORDER_LIVE_|ORDER_ALLOW_|ORDER_DETACH_GATE_|ORDER_LEADING_DIP_|ORDER_SONGSHAN_COPYTRADE_|ORDER_TIMED_LIMIT_|ORDER_EP_STAGED_GATE_|RUN_LEADING_DIP_|RUN_SONGSHAN_COPYTRADE_|RUN_TIMED_LIMIT_|RUN_EP_STAGED_GATE|RUN_RRG_C18ACC_|RUN_C18ACC_|RUN_DETACH_GATE|FUBON_FORCE_SUBPROCESS)' \
        "${src_env}" 2>/dev/null \
        | grep -Eiv '(PASSWORD|SECRET|TOKEN|CERT|KEY|PIN)=' || true
    fi
  } >"${dest}"
  chmod 600 "${dest}"
  echo "  order.env mirror → ${dest}"
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
  APP_SUPPORT="${app_support}"
  render_template "${src}" "${C18ACC_LAUNCHER}"
  chmod +x "${C18ACC_LAUNCHER}"
  sync_order_env_mirror
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

install_detach_gate_launcher() {
  local src="${LAUNCHD_SRC}/detach-gate-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  DETACH_GATE_LAUNCHER="${app_support}/detach-gate.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${DETACH_GATE_LAUNCHER}"
  chmod +x "${DETACH_GATE_LAUNCHER}"
}

install_leading_dip_launcher() {
  local src="${LAUNCHD_SRC}/leading-dip-poll-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  LEADING_DIP_LAUNCHER="${app_support}/leading-dip-poll.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${LEADING_DIP_LAUNCHER}"
  chmod +x "${LEADING_DIP_LAUNCHER}"
}

install_songshan_copytrade_launcher() {
  local src="${LAUNCHD_SRC}/songshan-copytrade-poll-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  SONGSHAN_COPYTRADE_LAUNCHER="${app_support}/songshan-copytrade-poll.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  APP_SUPPORT="${app_support}"
  render_template "${src}" "${SONGSHAN_COPYTRADE_LAUNCHER}"
  chmod +x "${SONGSHAN_COPYTRADE_LAUNCHER}"
}

install_timed_limit_launcher() {
  local src="${LAUNCHD_SRC}/timed-limit-orders-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  TIMED_LIMIT_LAUNCHER="${app_support}/timed-limit-orders.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  APP_SUPPORT="${app_support}"
  render_template "${src}" "${TIMED_LIMIT_LAUNCHER}"
  chmod +x "${TIMED_LIMIT_LAUNCHER}"
}

install_ep_staged_gate_launcher() {
  local src="${LAUNCHD_SRC}/expert-pool-staged-gate-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  EP_STAGED_GATE_LAUNCHER="${app_support}/expert-pool-staged-gate.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  APP_SUPPORT="${app_support}"
  render_template "${src}" "${EP_STAGED_GATE_LAUNCHER}"
  chmod +x "${EP_STAGED_GATE_LAUNCHER}"
}

install_winbond_expert_launcher() {
  local src="${LAUNCHD_SRC}/winbond-expert-pool-watch-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  WINBOND_EXPERT_LAUNCHER="${app_support}/winbond-expert-pool-watch.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${WINBOND_EXPERT_LAUNCHER}"
  chmod +x "${WINBOND_EXPERT_LAUNCHER}"
}

install_second_disp_expert_launcher() {
  local src="${LAUNCHD_SRC}/second-disp-expert-pool-watch-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  SECOND_DISP_EXPERT_LAUNCHER="${app_support}/second-disp-expert-pool-watch.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${SECOND_DISP_EXPERT_LAUNCHER}"
  chmod +x "${SECOND_DISP_EXPERT_LAUNCHER}"
}

install_expert_pool_chart_launcher() {
  local src="${LAUNCHD_SRC}/expert-pool-chart-digest-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  EXPERT_POOL_CHART_LAUNCHER="${app_support}/expert-pool-chart-digest.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${EXPERT_POOL_CHART_LAUNCHER}"
  chmod +x "${EXPERT_POOL_CHART_LAUNCHER}"
}

install_holdings_branch_sell_launcher() {
  local src="${LAUNCHD_SRC}/holdings-branch-sell-monitor-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  HOLDINGS_BRANCH_SELL_LAUNCHER="${app_support}/holdings-branch-sell-monitor.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${HOLDINGS_BRANCH_SELL_LAUNCHER}"
  chmod +x "${HOLDINGS_BRANCH_SELL_LAUNCHER}"
}

install_branch_tape_prewarm_launcher() {
  local src="${LAUNCHD_SRC}/branch-tape-prewarm-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  BRANCH_TAPE_PREWARM_LAUNCHER="${app_support}/branch-tape-prewarm.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${BRANCH_TAPE_PREWARM_LAUNCHER}"
  chmod +x "${BRANCH_TAPE_PREWARM_LAUNCHER}"
}

install_crash_thermometer_launcher() {
  local src="${LAUNCHD_SRC}/crash-thermometer-daily-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  CRASH_THERMOMETER_LAUNCHER="${app_support}/crash-thermometer-daily.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${CRASH_THERMOMETER_LAUNCHER}"
  chmod +x "${CRASH_THERMOMETER_LAUNCHER}"
}

install_ops_live_ta_launcher() {
  local src="${LAUNCHD_SRC}/ops-live-ta-poll-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  OPS_LIVE_TA_LAUNCHER="${app_support}/ops-live-ta-poll.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${OPS_LIVE_TA_LAUNCHER}"
  chmod +x "${OPS_LIVE_TA_LAUNCHER}"
}

install_ops_console_evening_launcher() {
  local src="${LAUNCHD_SRC}/ops-console-evening-sync-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.etf"
  OPS_CONSOLE_EVENING_LAUNCHER="${app_support}/ops-console-evening-sync.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${OPS_CONSOLE_EVENING_LAUNCHER}"
  chmod +x "${OPS_CONSOLE_EVENING_LAUNCHER}"
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
    "${src_dir}"/launchd_detach-gate* \
    "${src_dir}"/launchd_rrg-c18acc-poll* \
    "${src_dir}"/launchd_intraday-* \
    "${src_dir}"/launchd_rrg-mono-intraday-watch* \
    "${src_dir}"/launchd_vcp-funnel-specs* \
    "${src_dir}"/launchd_morning-holdings-brief* \
    "${src_dir}"/launchd_c18acc-extension-overlay* \
    "${src_dir}"/buy_signal_radar_*.log \
    "${src_dir}"/sell_signal_radar_*.log \
    "${src_dir}"/detach_gate_*.log \
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
  DETACH_GATE_LAUNCHER=""
  LEADING_DIP_LAUNCHER=""
  SONGSHAN_COPYTRADE_LAUNCHER=""
  TIMED_LIMIT_LAUNCHER=""
  EP_STAGED_GATE_LAUNCHER=""
  WINBOND_EXPERT_LAUNCHER=""
  SECOND_DISP_EXPERT_LAUNCHER=""
  EXPERT_POOL_CHART_LAUNCHER=""
  HOLDINGS_BRANCH_SELL_LAUNCHER=""
  BRANCH_TAPE_PREWARM_LAUNCHER=""
  CRASH_THERMOMETER_LAUNCHER=""
  OPS_LIVE_TA_LAUNCHER=""
  OPS_CONSOLE_EVENING_LAUNCHER=""
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
  install_buy_radar_launcher
  install_sell_radar_launcher
  install_detach_gate_launcher
  install_leading_dip_launcher
  install_songshan_copytrade_launcher
  install_timed_limit_launcher
  install_ep_staged_gate_launcher
  install_winbond_expert_launcher
  install_second_disp_expert_launcher
  install_expert_pool_chart_launcher
  install_holdings_branch_sell_launcher
  install_branch_tape_prewarm_launcher
  install_crash_thermometer_launcher
  install_ops_live_ta_launcher
  install_ops_console_evening_launcher
  # specialty-expert-pool-watch retired 2026-07-20 · merged into winbond-expert-pool-watch
  # morning-holdings-brief retired 2026-07-16 · not installed

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
    # calendar-branch sed may leave {{HOME}}; resolve for Library/Logs paths
    sed -i '' "s|{{HOME}}|${HOME}|g" "${dest}" 2>/dev/null \
      || sed -i "s|{{HOME}}|${HOME}|g" "${dest}"
    bootstrap_label "${dest}"
    echo "✓ ${label}"
  done

  mkdir -p "${HOME}/Library/Logs/com.jackm4.etf"

  echo ""
  verify_documents_launch
  echo ""
  echo "完成。檢查："
  echo "  launchctl list | grep jackm4.etf"
  echo "  # launchd stdout（避開 Documents TCC）：~/Library/Logs/com.jackm4.etf/"
  echo "  # 業務 tick log 仍在：${PROJECT_ROOT}/logs/intraday/"
  echo "  tail -f ${PROJECT_ROOT}/logs/intraday/leading_dip_\$(date +%Y%m%d).log"
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
