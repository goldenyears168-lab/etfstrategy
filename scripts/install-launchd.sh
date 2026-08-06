#!/usr/bin/env bash
# 安裝下單層 launchd（Leading Dip · Songshan · sell/exit ops）
# ABC Order 已退役（2026-07-15）。C18acc 排程已退役（2026-08-04 · 策略不再採用）。
# Facts / Regime / VCP / digest 等已退役排程（可手動跑）。
#
# 用法：
#   scripts/install-launchd.sh           # 安裝並載入
#   scripts/install-launchd.sh --uninstall
#   scripts/install-launchd.sh --status

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCHD_SRC="${PROJECT_ROOT}/launchd"
APP_SUPPORT="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
AGENT_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"

# 研究層 collect job（fubon-premarket/fubon-intraday-quote-collect）刻意不在此陣列：plist 佔位符無 launcher template，安裝方式見各 scripts/launchd/*collect*.command 檔頭（一次性手動 sed + bootstrap）。
# （pre-market-auction-collect 已於 2026-08-01 退役，見 RETIRED_LABELS。）
LABELS=(
  com.jackm4.goldenstocks.buy-signal-radar
  com.jackm4.goldenstocks.detach-gate
  com.jackm4.goldenstocks.leading-dip-poll
  com.jackm4.goldenstocks.songshan-copytrade-poll
  com.jackm4.goldenstocks.tmf-channel-poll
  com.jackm4.goldenstocks.tmf-sim-server
  com.jackm4.goldenstocks.expert-pool-staged-gate
  com.jackm4.goldenstocks.nightly-expert-digest
  com.jackm4.goldenstocks.second-disp-expert-pool-watch
  com.jackm4.goldenstocks.expert-pool-chart-digest
  com.jackm4.goldenstocks.holdings-branch-sell-monitor
  com.jackm4.goldenstocks.branch-tape-prewarm
  com.jackm4.goldenstocks.ops-console-evening-sync
  com.jackm4.goldenstocks.mini-schedule
)

TEMPLATES=(
  com.jackm4.goldenstocks.buy-signal-radar.plist.template
  com.jackm4.goldenstocks.detach-gate.plist.template
  com.jackm4.goldenstocks.leading-dip-poll.plist.template
  com.jackm4.goldenstocks.songshan-copytrade-poll.plist.template
  com.jackm4.goldenstocks.tmf-channel-poll.plist.template
  com.jackm4.goldenstocks.tmf-sim-server.plist.template
  com.jackm4.goldenstocks.expert-pool-staged-gate.plist.template
  com.jackm4.goldenstocks.nightly-expert-digest.plist.template
  com.jackm4.goldenstocks.second-disp-expert-pool-watch.plist.template
  com.jackm4.goldenstocks.expert-pool-chart-digest.plist.template
  com.jackm4.goldenstocks.holdings-branch-sell-monitor.plist.template
  com.jackm4.goldenstocks.branch-tape-prewarm.plist.template
  com.jackm4.goldenstocks.ops-console-evening-sync.plist.template
  com.jackm4.goldenstocks.mini-schedule.plist.template
)

usage() {
  cat <<EOF
用法: $(basename "$0") [--uninstall|--status]

  預設：將 launchd/*.plist.template 渲染後安裝到
        ~/Library/LaunchAgents/ 並 launchctl load。

  現行排程（Order layer · Leading Dip / Songshan；本地時間）：
    buy-signal-radar        週一至五 09:00–13:20 每 5 分（notify；ABC Order 已退役）
    detach-gate             週一至五 09:40–12:30 每 5 分（台美脫鉤閘門 · 半倉買一）
    leading-dip-poll        週一至五 09:05–13:25 每 5 分（Leading Dip · 獨立袖套 · 預設 dry-run）
    songshan-copytrade-poll 週一至五 09:25–09:40 每 5 分（跟單松山 5d淨比95∩!mega+25m nonfail · 預算制約10萬）
    tmf-channel-poll        KeepAlive worker（日盤+夜盤 · TMF · 重用 session · 預設 dry-run）
    tmf-sim-server          KeepAlive（TMF paper UI :8770 · 不下單 · PYTHONPATH=src 用新引擎）
    expert-pool-staged-gate 週一至五 09:00／01／05／25（專家池 gap→05→25 漏斗閘門 · 預設 dry-run）
    nightly-expert-digest  週一至五 20:00（專家池+松山+新店 輕量 digest · 不下單）
    second-disp-expert-pool-watch  週一至五 20:35（處置股專家池跟單 · T0濾網 · 不下單）
    expert-pool-chart-digest   週一至五 20:05（專家池達標 HTML 圖文 · 無達標不寄 · 不下單）
    holdings-branch-sell-monitor  週一至五 20:10（富邦持倉×專家分點淨賣預警 · 不下單）
    branch-tape-prewarm        週一至五 18:30（分點 tape 補檔 POOLS∪持倉 · 讓 20:00 起純讀 DB · 不下單）
    ops-console-evening-sync   週一至五 20:40（ops.snapshots／sleeve／holdings 上牆 · 不下單）
    mini-schedule              每日 08:30（headless Claude 資料體檢 · 唯讀 DB · 不下單）
                               判準 SSOT：scripts/launchd/mini-schedule-prompt.txt
                               cwd 為 GOLDENSTOCKS_DATA_DIR（護欄＝該目錄 CLAUDE.md）

  已退役（不再安裝；手動仍可用對應 python）：
    rrg-c18acc-poll（2026-08-04 退役 · C18acc 策略不再採用 · order 層 python 保留 ·
                     plist／launcher 備份於 goldenstocks-data/.retired-launchd-backup-*）·
    pre-market-auction-collect · ops-live-ta-poll · live-ta-kbar-sync · sell-signal-radar（2026-08-01 退役 · python 保留 · 配備/範本備份於 goldenstocks-data/.retired-launchd-backup-*）·
    specialty-expert-pool-watch（已併入 nightly-expert-digest 統一入口）·
    morning-holdings-brief（2026-07-16 退役 · 手動仍可用 scripts/order/morning_holdings_brief.py）·
    ABC v3+f1 Order（buy-radar 不再送單）·
    intraday-exit-gate（結構停損閘門 · 已退回 Research）·
    evening-holdings · digests · vcp-funnel-specs · minervini-sepa-basket ·
    mutual-fund-disclosure-watch · rrg-mono-intraday-watch · weekly-deep ·
    c18acc-extension-overlay

  log：盤中 ${PROJECT_ROOT}/logs/intraday/

  注意：Mac 須已登入。盤中 poll 用 StartInterval=300（非 Aqua CalendarInterval），
        開盤窗由 launcher 過濾；order-wake 每 5 分 caffeinate。launchd stdout 寫
        ~/Library/Logs/com.jackm4.goldenstocks/（避開 Documents TCC → EX_CONFIG）。
        leading-dip-poll · buy/sell radar · detach-gate
        以 Application Support launcher + /bin/bash 背景執行。
EOF
}

LAUNCHD_COMMANDS=(
  buy-signal-radar
  detach-gate
  leading-dip-poll
  songshan-copytrade-poll
  tmf-channel-poll
  expert-pool-staged-gate
  nightly-expert-digest
  second-disp-expert-pool-watch
  expert-pool-chart-digest
  holdings-branch-sell-monitor
  branch-tape-prewarm
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
  local probe_log="/tmp/com.jackm4.goldenstocks.tcc-probe.log"
  cat >"${probe}" <<'PROBE'
#!/bin/bash
echo OK > /tmp/com.jackm4.goldenstocks.tcc-probe.log
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
  com.jackm4.goldenstocks.morning-holdings-brief
  com.jackm4.goldenstocks.rrg-mono-scan
  com.jackm4.goldenstocks.vcp-intraday-watch
  com.jackm4.goldenstocks.morning-regime
  com.jackm4.goldenstocks.test-doc-bash
  com.jackm4.goldenstocks.c18acc-extension-overlay
  com.jackm4.goldenstocks.weekly-deep
  com.jackm4.goldenstocks.rrg-mono-intraday-watch
  com.jackm4.goldenstocks.evening-holdings
  com.jackm4.goldenstocks.mutual-fund-disclosure-watch
  com.jackm4.goldenstocks.intraday-open-digest
  com.jackm4.goldenstocks.intraday-midday-digest
  com.jackm4.goldenstocks.intraday-1300-digest
  com.jackm4.goldenstocks.vcp-funnel-specs
  com.jackm4.goldenstocks.minervini-sepa-basket
  com.jackm4.goldenstocks.intraday-exit-gate
  com.jackm4.goldenstocks.specialty-expert-pool-watch
  # 2026-08-01 退役（保留 python；見 goldenstocks-data/.retired-launchd-backup-*）
  com.jackm4.goldenstocks.pre-market-auction-collect
  com.jackm4.goldenstocks.ops-live-ta-poll
  com.jackm4.goldenstocks.live-ta-kbar-sync
  com.jackm4.goldenstocks.sell-signal-radar
  # 2026-08-02 退役（訊號經驗證無效 31-35% · python 保留）
  com.jackm4.goldenstocks.crash-thermometer-daily
  # 2026-08-02 mini 每日體檢搬入 repo 版控：舊手裝 label → 新 mini-schedule
  com.goldenstocks.mini
  # 2026-08-04 退役（C18acc 策略不再採用 · src/order/c18acc_* python 保留）
  com.jackm4.goldenstocks.rrg-c18acc-poll
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

generate_buy_radar_calendar_intervals() {
  local out="/tmp/com.jackm4.goldenstocks.buy-radar-calendar.xml"
  generate_five_minute_clock_calendar "${out}"
  BUY_RADAR_CALENDAR_INTERVALS_FILE="${out}"
}

generate_sell_radar_calendar_intervals() {
  local out="/tmp/com.jackm4.goldenstocks.sell-radar-calendar.xml"
  generate_five_minute_clock_calendar "${out}"
  SELL_RADAR_CALENDAR_INTERVALS_FILE="${out}"
}

generate_detach_gate_calendar_intervals() {
  local out="/tmp/com.jackm4.goldenstocks.detach-gate-calendar.xml"
  generate_five_minute_clock_calendar "${out}"
  DETACH_GATE_CALENDAR_INTERVALS_FILE="${out}"
}

generate_leading_dip_calendar_intervals() {
  local out="/tmp/com.jackm4.goldenstocks.leading-dip-calendar.xml"
  generate_five_minute_clock_calendar "${out}"
  LEADING_DIP_CALENDAR_INTERVALS_FILE="${out}"
}

generate_songshan_copytrade_calendar_intervals() {
  local out="/tmp/com.jackm4.goldenstocks.songshan-copytrade-calendar.xml"
  generate_five_minute_clock_calendar "${out}"
  SONGSHAN_COPYTRADE_CALENDAR_INTERVALS_FILE="${out}"
}

render_template() {
  local template="$1"
  local dest="$2"
  if grep -q '{{BUY_RADAR_CALENDAR_INTERVALS}}' "${template}"; then
    generate_buy_radar_calendar_intervals
    sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
        -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
        -e "s|{{DETACH_GATE_LAUNCHER}}|${DETACH_GATE_LAUNCHER}|g" \
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
        -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
        -e "s|{{DETACH_GATE_LAUNCHER}}|${DETACH_GATE_LAUNCHER}|g" \
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
        -e "s|{{APP_SUPPORT}}|${APP_SUPPORT:-${HOME}/Library/Application Support/com.jackm4.goldenstocks}|g" \
        -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
        -e "s|{{DETACH_GATE_LAUNCHER}}|${DETACH_GATE_LAUNCHER}|g" \
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
        -e "s|{{APP_SUPPORT}}|${APP_SUPPORT:-${HOME}/Library/Application Support/com.jackm4.goldenstocks}|g" \
        -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
        -e "s|{{DETACH_GATE_LAUNCHER}}|${DETACH_GATE_LAUNCHER}|g" \
        -e "s|{{LEADING_DIP_LAUNCHER}}|${LEADING_DIP_LAUNCHER}|g" \
        -e "s|{{SONGSHAN_COPYTRADE_LAUNCHER}}|${SONGSHAN_COPYTRADE_LAUNCHER}|g" \
        -e "s|{{EP_STAGED_GATE_LAUNCHER}}|${EP_STAGED_GATE_LAUNCHER}|g" \
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
        -e "s|{{APP_SUPPORT}}|${APP_SUPPORT:-${HOME}/Library/Application Support/com.jackm4.goldenstocks}|g" \
        -e "s|{{SONGSHAN_COPYTRADE_LAUNCHER}}|${SONGSHAN_COPYTRADE_LAUNCHER}|g" \
        -e "s|{{EP_STAGED_GATE_LAUNCHER}}|${EP_STAGED_GATE_LAUNCHER}|g" \
        -e "s|{{LEADING_DIP_LAUNCHER}}|${LEADING_DIP_LAUNCHER}|g" \
        "${template}" \
      | sed "/{{SONGSHAN_COPYTRADE_CALENDAR_INTERVALS}}/r ${SONGSHAN_COPYTRADE_CALENDAR_INTERVALS_FILE}" \
      | sed '/{{SONGSHAN_COPYTRADE_CALENDAR_INTERVALS}}/d' \
      >"${dest}"
    return
  fi
  sed -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
      -e "s|{{APP_SUPPORT}}|${APP_SUPPORT:-${HOME}/Library/Application Support/com.jackm4.goldenstocks}|g" \
      -e "s|{{HOME}}|${HOME}|g" \
      -e "s|{{BUY_RADAR_LAUNCHER}}|${BUY_RADAR_LAUNCHER}|g" \
      -e "s|{{DETACH_GATE_LAUNCHER}}|${DETACH_GATE_LAUNCHER}|g" \
      -e "s|{{LEADING_DIP_LAUNCHER}}|${LEADING_DIP_LAUNCHER}|g" \
      -e "s|{{SONGSHAN_COPYTRADE_LAUNCHER}}|${SONGSHAN_COPYTRADE_LAUNCHER}|g" \
      -e "s|{{TMF_CHANNEL_LAUNCHER}}|${TMF_CHANNEL_LAUNCHER}|g" \
      -e "s|{{EP_STAGED_GATE_LAUNCHER}}|${EP_STAGED_GATE_LAUNCHER}|g" \
      -e "s|{{NIGHTLY_EXPERT_DIGEST_LAUNCHER}}|${NIGHTLY_EXPERT_DIGEST_LAUNCHER}|g" \
      -e "s|{{SECOND_DISP_EXPERT_LAUNCHER}}|${SECOND_DISP_EXPERT_LAUNCHER}|g" \
      -e "s|{{EXPERT_POOL_CHART_LAUNCHER}}|${EXPERT_POOL_CHART_LAUNCHER}|g" \
      -e "s|{{HOLDINGS_BRANCH_SELL_LAUNCHER}}|${HOLDINGS_BRANCH_SELL_LAUNCHER}|g" \
      -e "s|{{BRANCH_TAPE_PREWARM_LAUNCHER}}|${BRANCH_TAPE_PREWARM_LAUNCHER}|g" \
      -e "s|{{OPS_CONSOLE_EVENING_LAUNCHER}}|${OPS_CONSOLE_EVENING_LAUNCHER}|g" \
      -e "s|{{MINI_SCHEDULE_LAUNCHER}}|${MINI_SCHEDULE_LAUNCHER}|g" \
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
  # Mirror non-secret ORDER_/RUN_ keys for launchd (Documents .env may be TCC-blocked).
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
  local src_env="${PROJECT_ROOT}/.env"
  local dest="${app_support}/order.env"
  mkdir -p "${app_support}"
  {
    echo "# Generated by install-launchd.sh · $(date '+%Y-%m-%d %H:%M:%S')"
    echo "# Whitelist only · no passwords / cert paths"
    if [[ -f "${src_env}" ]]; then
      # shellcheck disable=SC2016
      grep -E '^(ORDER_MASTER_ENABLED|ORDER_RESERVED_CASH_|ORDER_LIVE_|ORDER_ALLOW_|ORDER_DETACH_GATE_|ORDER_LEADING_DIP_|ORDER_SONGSHAN_COPYTRADE_|ORDER_EP_STAGED_GATE_|ORDER_TMF_CHANNEL_|ORDER_TMF_ACCOUNT|RUN_LEADING_DIP_|RUN_SONGSHAN_COPYTRADE_|RUN_EP_STAGED_GATE|RUN_DETACH_GATE|FUBON_FORCE_SUBPROCESS)' \
        "${src_env}" 2>/dev/null \
        | grep -Eiv '(PASSWORD|SECRET|TOKEN|CERT|KEY|PIN)=' || true
    fi
  } >"${dest}"
  chmod 600 "${dest}"
  echo "  order.env mirror → ${dest}"
}

install_buy_radar_launcher() {
  local src="${LAUNCHD_SRC}/buy-signal-radar-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
  BUY_RADAR_LAUNCHER="${app_support}/buy-signal-radar.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${BUY_RADAR_LAUNCHER}"
  chmod +x "${BUY_RADAR_LAUNCHER}"
}

install_detach_gate_launcher() {
  local src="${LAUNCHD_SRC}/detach-gate-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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

install_tmf_channel_launcher() {
  local src="${LAUNCHD_SRC}/tmf-channel-poll-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
  TMF_CHANNEL_LAUNCHER="${app_support}/tmf-channel-poll.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  if [[ ! -x "${PROJECT_ROOT}/.venv-fubon/bin/python" ]]; then
    echo "✗ tmf-channel-poll 需要 ${PROJECT_ROOT}/.venv-fubon/bin/python" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  APP_SUPPORT="${app_support}"
  render_template "${src}" "${TMF_CHANNEL_LAUNCHER}"
  chmod +x "${TMF_CHANNEL_LAUNCHER}"
}

install_ep_staged_gate_launcher() {
  local src="${LAUNCHD_SRC}/expert-pool-staged-gate-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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

install_nightly_expert_digest_launcher() {
  local src="${LAUNCHD_SRC}/nightly-expert-digest-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
  NIGHTLY_EXPERT_DIGEST_LAUNCHER="${app_support}/nightly-expert-digest.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${NIGHTLY_EXPERT_DIGEST_LAUNCHER}"
  chmod +x "${NIGHTLY_EXPERT_DIGEST_LAUNCHER}"
}

install_second_disp_expert_launcher() {
  local src="${LAUNCHD_SRC}/second-disp-expert-pool-watch-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
  BRANCH_TAPE_PREWARM_LAUNCHER="${app_support}/branch-tape-prewarm.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${BRANCH_TAPE_PREWARM_LAUNCHER}"
  chmod +x "${BRANCH_TAPE_PREWARM_LAUNCHER}"
}

install_ops_console_evening_launcher() {
  local src="${LAUNCHD_SRC}/ops-console-evening-sync-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
  OPS_CONSOLE_EVENING_LAUNCHER="${app_support}/ops-console-evening-sync.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${OPS_CONSOLE_EVENING_LAUNCHER}"
  chmod +x "${OPS_CONSOLE_EVENING_LAUNCHER}"
}

install_mini_schedule_launcher() {
  local src="${LAUNCHD_SRC}/mini-schedule-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
  MINI_SCHEDULE_LAUNCHER="${app_support}/mini-schedule.sh"
  if [[ ! -f "${src}" ]]; then
    echo "✗ 缺少 ${src}" >&2
    exit 1
  fi
  # 判準 SSOT：prompt 進 repo 後，改判準 = 改這支檔案（不是改程式）
  if [[ ! -f "${PROJECT_ROOT}/scripts/launchd/mini-schedule-prompt.txt" ]]; then
    echo "✗ 缺少 ${PROJECT_ROOT}/scripts/launchd/mini-schedule-prompt.txt" >&2
    exit 1
  fi
  mkdir -p "${app_support}"
  render_template "${src}" "${MINI_SCHEDULE_LAUNCHER}"
  chmod +x "${MINI_SCHEDULE_LAUNCHER}"
}

install_evening_holdings_launcher() {
  local src="${LAUNCHD_SRC}/evening-holdings-launcher.sh.template"
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  local app_support="${HOME}/Library/Application Support/com.jackm4.goldenstocks"
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
  BUY_RADAR_LAUNCHER=""
  DETACH_GATE_LAUNCHER=""
  LEADING_DIP_LAUNCHER=""
  SONGSHAN_COPYTRADE_LAUNCHER=""
  TMF_CHANNEL_LAUNCHER=""
  EP_STAGED_GATE_LAUNCHER=""
  NIGHTLY_EXPERT_DIGEST_LAUNCHER=""
  SECOND_DISP_EXPERT_LAUNCHER=""
  EXPERT_POOL_CHART_LAUNCHER=""
  HOLDINGS_BRANCH_SELL_LAUNCHER=""
  BRANCH_TAPE_PREWARM_LAUNCHER=""
  OPS_CONSOLE_EVENING_LAUNCHER=""
  MINI_SCHEDULE_LAUNCHER=""
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
  sync_order_env_mirror
  install_buy_radar_launcher
  install_detach_gate_launcher
  install_leading_dip_launcher
  install_songshan_copytrade_launcher
  install_tmf_channel_launcher
  install_ep_staged_gate_launcher
  install_nightly_expert_digest_launcher
  install_second_disp_expert_launcher
  install_expert_pool_chart_launcher
  install_holdings_branch_sell_launcher
  install_branch_tape_prewarm_launcher
  install_ops_console_evening_launcher
  install_mini_schedule_launcher
  # specialty-expert-pool-watch retired 2026-07-20 · merged into nightly-expert-digest
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
    # Guard: launchd cannot open StandardOut/ErrorPath under ~/Documents (TCC →
    # silent EX_CONFIG 78, job never spawns). Fail loudly instead of shipping it.
    local _sp
    for _sp in StandardOutPath StandardErrorPath; do
      local _v
      _v="$(/usr/bin/plutil -extract "${_sp}" raw -o - "${dest}" 2>/dev/null || true)"
      if [[ "${_v}" == "${HOME}/Documents/"* || "${_v}" == *"/Documents/"*"/logs/"* ]]; then
        echo "✗ ${label}: ${_sp}=${_v} 落在 ~/Documents（TCC→EX_CONFIG 78）；請改用 ~/Library/Logs/com.jackm4.goldenstocks/" >&2
        exit 1
      fi
    done
    bootstrap_label "${dest}"
    echo "✓ ${label}"
  done

  # TMF is the live order sleeve · enable so reboot/reinstall does not leave it disabled
  launchctl enable "${GUI_DOMAIN}/com.jackm4.goldenstocks.tmf-channel-poll" 2>/dev/null || true
  launchctl kickstart -k "${GUI_DOMAIN}/com.jackm4.goldenstocks.tmf-channel-poll" 2>/dev/null || true

  mkdir -p "${HOME}/Library/Logs/com.jackm4.goldenstocks"

  echo ""
  verify_documents_launch
  echo ""
  echo "完成。檢查："
  echo "  launchctl list | grep jackm4"
  echo "  # launchd stdout（避開 Documents TCC）：~/Library/Logs/com.jackm4.goldenstocks/"
  echo "  # TMF tick log：\${GOLDENSTOCKS_DATA_DIR:-~/goldenstocks-data}/logs/intraday/tmf_channel_live_\$(date +%Y%m%d).log"
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
  launchctl list 2>/dev/null | grep -E 'jackm4\.etf' || echo "  （無已載入的 com.jackm4.goldenstocks.*）"
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
