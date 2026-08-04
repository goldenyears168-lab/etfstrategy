#!/usr/bin/env bash
# launchd 排程健檢儀表板（唯讀 · 供 .command 或終端機呼叫）
#
# 用法：
#   scripts/launchd_status_dashboard.sh
#   scripts/launchd_status_dashboard.sh --json   # 機器可讀（未來擴充）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
AGENT_DIR="${HOME}/Library/LaunchAgents"
TODAY_YMD="$(date '+%Y-%m-%d')"
TODAY_COMPACT="$(date '+%Y%m%d')"
WEEKDAY="$(date '+%u')"   # 1=Mon … 7=Sun
NOW_HM="$(date '+%H:%M')"
NOW_TS="$(date '+%Y-%m-%d %H:%M:%S')"

# label|顯示名|排程說明|log 檔（相對 ROOT）|適用日（1-5=週一至五,7=週日,0=全部）
# 現行 = Order layer（Leading Dip / Songshan / sell / detach）；digest / evening / VCP / exit-gate 等已退役
# ABC Order 已退役 2026-07-15 · C18acc poll 已退役 2026-08-04（策略不再採用）
JOBS=(
  "com.jackm4.goldenstocks.order-wake|下單防睡眠|週一至五 08:55|logs/launchd_order-wake.log|1-5"
  "com.jackm4.goldenstocks.order-chase-open|開盤追價|週一至五 09:00–09:04 每分鐘|logs/launchd_order-chase-open.log|1-5"
  "com.jackm4.goldenstocks.buy-signal-radar|Buy signal radar|週一至五 09:00–13:20 每 5 分|logs/intraday/launchd_buy-signal-radar.log|1-5"
  "com.jackm4.goldenstocks.sell-signal-radar|Sell signal radar|週一至五 09:06–13:20 每 5 分|logs/intraday/launchd_sell-signal-radar.log|1-5"
  "com.jackm4.goldenstocks.detach-gate|Detach Gate 台美脫鉤|週一至五 09:40–12:30 每 5 分|logs/intraday/launchd_detach-gate.log|1-5"
  "com.jackm4.goldenstocks.leading-dip-poll|Leading Dip Order|週一至五 09:05–13:25 每 5 分|logs/intraday/launchd_leading-dip-poll.log|1-5"
  "com.jackm4.goldenstocks.songshan-copytrade-poll|跟單松山 Order|週一至五 09:25–09:40 每 5 分|logs/intraday/launchd_songshan-copytrade-poll.log|1-5"
)

USE_COLOR=1
if [[ -n "${NO_COLOR:-}" ]] || [[ ! -t 1 ]]; then
  USE_COLOR=0
fi

c() {
  if [[ "${USE_COLOR}" -eq 1 ]]; then
    printf '\033[%sm' "$1"
  fi
}
reset() {
  if [[ "${USE_COLOR}" -eq 1 ]]; then
    printf '\033[0m'
  fi
}

hr() {
  printf '%s\n' "────────────────────────────────────────────────────────────────────────"
}

weekday_name() {
  case "$1" in
    1) echo "週一" ;;
    2) echo "週二" ;;
    3) echo "週三" ;;
    4) echo "週四" ;;
    5) echo "週五" ;;
    6) echo "週六" ;;
    7) echo "週日" ;;
    *) echo "?" ;;
  esac
}

job_active_today() {
  local days="$1"
  case "${days}" in
    1-5) [[ "${WEEKDAY}" -ge 1 && "${WEEKDAY}" -le 5 ]] ;;
    7) [[ "${WEEKDAY}" -eq 7 ]] ;;
    *) return 0 ;;
  esac
}

launchctl_line() {
  local label="$1"
  launchctl list 2>/dev/null | awk -v lbl="${label}" '$3 == lbl { print $1, $2; exit }'
}

parse_last_run() {
  local log_path="$1"
  [[ -f "${log_path}" ]] || return 1
  local line
  line="$(grep -E '(開始|結束|end exit=|結束 exit=)' "${log_path}" 2>/dev/null | tail -1 || true)"
  [[ -n "${line}" ]] || return 1
  printf '%s' "${line}"
}

parse_exit_from_line() {
  local line="$1"
  if [[ "${line}" =~ exit=([0-9]+) ]]; then
    echo "${BASH_REMATCH[1]}"
  elif [[ "${line}" =~ 結束[[:space:]]*$ ]]; then
    echo "0"
  else
    echo "?"
  fi
}

parse_time_from_line() {
  local line="$1"
  if [[ "${line}" =~ ([0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]][0-9]{2}:[0-9]{2}:[0-9]{2}) ]]; then
    echo "${BASH_REMATCH[1]}"
  else
    echo "—"
  fi
}

count_runs_today() {
  local log_path="$1"
  [[ -f "${log_path}" ]] || { echo "0"; return; }
  grep -c "${TODAY_YMD}" "${log_path}" 2>/dev/null || echo "0"
}

last_exit_today() {
  local log_path="$1"
  [[ -f "${log_path}" ]] || { echo ""; return; }
  local line
  line="$(grep "${TODAY_YMD}" "${log_path}" 2>/dev/null | grep -E '(結束|end exit=)' | tail -1 || true)"
  if [[ -n "${line}" ]]; then
    parse_exit_from_line "${line}"
  else
    echo ""
  fi
}

status_badge() {
  local installed="$1"
  local active_today="$2"
  local last_exit_today="$3"
  local last_exit_any="$4"
  local runs_today="$5"

  if [[ "${installed}" -eq 0 ]]; then
    printf '%s' "未安裝"
    return
  fi
  if [[ "${active_today}" -eq 0 ]]; then
    printf '%s' "今日休"
    return
  fi
  if [[ -n "${last_exit_today}" ]]; then
    if [[ "${last_exit_today}" == "0" ]]; then
      if [[ "${runs_today}" -gt 1 ]]; then
        printf '%s' "OK×${runs_today}"
      else
        printf '%s' "OK"
      fi
    else
      printf '%s' "FAIL(${last_exit_today})"
    fi
    return
  fi
  if [[ -n "${last_exit_any}" && "${last_exit_any}" != "0" ]]; then
    printf '%s' "待跑(前次FAIL)"
    return
  fi
  printf '%s' "待跑"
}

print_header() {
  hr
  printf '%b' "$(c "1;36")"
  printf '  ETF launchd 排程健檢儀表板\n'
  printf '%b' "$(reset)"
  printf '  %s · %s · 現在 %s\n' "${TODAY_YMD}" "$(weekday_name "${WEEKDAY}")" "${NOW_TS}"
  printf '  專案：%s\n' "${ROOT}"
  hr
}

print_summary() {
  local installed=0 loaded=0 fail_today=0 ok_today=0 pending=0 off_today=0
  local label name schedule log_rel days log_path
  local lc_pid lc_exit active_today runs_today exit_today last_line last_exit

  for entry in "${JOBS[@]}"; do
    IFS='|' read -r label name schedule log_rel days <<<"${entry}"
    log_path="${ROOT}/${log_rel}"

    if [[ -f "${AGENT_DIR}/${label}.plist" ]]; then
      installed=$((installed + 1))
      if launchctl_line "${label}" | grep -q .; then
        loaded=$((loaded + 1))
      fi
    fi

    if job_active_today "${days}"; then
      active_today=1
    else
      active_today=0
      off_today=$((off_today + 1))
      continue
    fi

    runs_today="$(count_runs_today "${log_path}")"
    exit_today="$(last_exit_today "${log_path}")"
    last_line="$(parse_last_run "${log_path}" || true)"
    last_exit=""
    [[ -n "${last_line}" ]] && last_exit="$(parse_exit_from_line "${last_line}")"

    if [[ -n "${exit_today}" ]]; then
      if [[ "${exit_today}" == "0" ]]; then
        ok_today=$((ok_today + 1))
      else
        fail_today=$((fail_today + 1))
      fi
    else
      pending=$((pending + 1))
    fi
  done

  printf '\n'
  printf '%b摘要%b  已安裝 %d · 已載入 %d · 今日 OK %d · FAIL %d · 待跑 %d · 休市 %d\n' \
    "$(c "1")" "$(reset)" \
    "${installed}" "${loaded}" "${ok_today}" "${fail_today}" "${pending}" "${off_today}"
}

print_alerts() {
  local any=0
  local label name schedule log_rel days log_path
  local exit_today last_line err_log err_tail

  for entry in "${JOBS[@]}"; do
    IFS='|' read -r label name schedule log_rel days <<<"${entry}"
    job_active_today "${days}" || continue
    [[ -f "${AGENT_DIR}/${label}.plist" ]] || continue

    log_path="${ROOT}/${log_rel}"
    exit_today="$(last_exit_today "${log_path}")"
    if [[ -n "${exit_today}" && "${exit_today}" != "0" ]]; then
      if [[ "${any}" -eq 0 ]]; then
        printf '\n%b今日警示%b\n' "$(c "1;31")" "$(reset)"
        any=1
      fi
      last_line="$(parse_last_run "${log_path}" || true)"
      printf '  ✗ %s  exit=%s  %s\n' "${name}" "${exit_today}" "$(parse_time_from_line "${last_line}")"
      err_log="${log_path%.log}.err.log"
      if [[ -f "${err_log}" ]]; then
        err_tail="$(grep "${TODAY_YMD}" "${err_log}" 2>/dev/null | tail -1 || tail -1 "${err_log}" 2>/dev/null || true)"
        if [[ -n "${err_tail}" ]]; then
          printf '      err: %s\n' "${err_tail}"
        fi
      fi
    fi
  done

  if [[ "${any}" -eq 0 ]]; then
    printf '\n%b今日警示%b  （無 FAIL 紀錄）\n' "$(c "1;32")" "$(reset)"
  fi
}

print_job_table() {
  printf '\n%b排程一覽%b\n' "$(c "1")" "$(reset)"
  printf '%-28s %-6s %-12s %-19s %-10s\n' "任務" "Agent" "今日" "最近執行" "排程"
  hr

  local label name schedule log_rel days log_path
  local installed lc_pid lc_exit active_today runs_today exit_today last_line last_time last_exit badge agent_st

  for entry in "${JOBS[@]}"; do
    IFS='|' read -r label name schedule log_rel days <<<"${entry}"
    log_path="${ROOT}/${log_rel}"

    if [[ -f "${AGENT_DIR}/${label}.plist" ]]; then
      installed=1
      read -r lc_pid lc_exit <<<"$(launchctl_line "${label}")" || true
      if [[ -n "${lc_pid:-}" ]]; then
        if [[ "${lc_pid}" == "-" ]]; then
          agent_st="idle"
        else
          agent_st="run"
        fi
      else
        agent_st="—"
        lc_exit="—"
      fi
    else
      installed=0
      agent_st="—"
      lc_exit="—"
    fi

    if job_active_today "${days}"; then
      active_today=1
    else
      active_today=0
    fi

    runs_today="$(count_runs_today "${log_path}")"
    exit_today="$(last_exit_today "${log_path}")"
    last_line="$(parse_last_run "${log_path}" || true)"
    last_exit=""
    [[ -n "${last_line}" ]] && last_exit="$(parse_exit_from_line "${last_line}")"
    last_time="$(parse_time_from_line "${last_line}")"

    badge="$(status_badge "${installed}" "${active_today}" "${exit_today}" "${last_exit}" "${runs_today}")"

    if [[ "${installed}" -eq 0 ]]; then
      printf '%-28s %b%-6s%b %-12s %-19s %s\n' \
        "${name}" "$(c "2")" "—" "$(reset)" "${badge}" "${last_time}" "${schedule}"
    elif [[ "${badge}" == FAIL* ]]; then
      printf '%-28s %-6s %b%-12s%b %-19s %s\n' \
        "${name}" "${agent_st}" "$(c "31")" "${badge}" "$(reset)" "${last_time}" "${schedule}"
    elif [[ "${badge}" == OK* ]]; then
      printf '%-28s %-6s %b%-12s%b %-19s %s\n' \
        "${name}" "${agent_st}" "$(c "32")" "${badge}" "$(reset)" "${last_time}" "${schedule}"
    else
      printf '%-28s %-6s %-12s %-19s %s\n' \
        "${name}" "${agent_st}" "${badge}" "${last_time}" "${schedule}"
    fi
  done
}

print_today_timeline() {
  if [[ "${WEEKDAY}" -lt 1 || "${WEEKDAY}" -gt 5 ]]; then
    if [[ "${WEEKDAY}" -eq 7 ]]; then
      printf '\n%b今日剩餘排程%b\n' "$(c "1")" "$(reset)"
      printf '  （週日無自動排程；weekly-deep 已退役）\n'
    fi
    return
  fi

  printf '\n%b今日剩餘排程%b（週一至五 · 現在 %s）\n' "$(c "1")" "$(reset)" "${NOW_HM}"
  local slots=(
    "09:00|Buy radar 開始"
    "09:05|Leading Dip poll 開始"
    "09:06|Sell radar 開始"
    "09:40|Detach Gate 開始"
  )
  local slot hm desc shown=0
  for slot in "${slots[@]}"; do
    hm="${slot%%|*}"
    desc="${slot#*|}"
    if [[ "${hm}" > "${NOW_HM}" ]]; then
      printf '  %s  %s\n' "${hm}" "${desc}"
      shown=$((shown + 1))
    fi
  done
  if [[ "${shown}" -eq 0 ]]; then
    printf '  （盤後定時排程已過；09:00–13:30 高頻 radar 若 Agent 正常仍會每 5 分觸發）\n'
  fi
  printf '  … 09:00–13:30  Buy / Sell radar 每 5 分 · Detach Gate 09:40–12:30\n'
}

print_log_hints() {
  printf '\n%b快速追 log%b\n' "$(c "1")" "$(reset)"
  printf '  tail -f %s/logs/intraday/launchd_buy-signal-radar.log\n' "${ROOT}"
  printf '  tail -f %s/logs/intraday/launchd_sell-signal-radar.log\n' "${ROOT}"
  printf '  tail -f %s/logs/intraday/launchd_detach-gate.log\n' "${ROOT}"
  printf '  tail -f %s/logs/intraday/launchd_leading-dip-poll.log\n' "${ROOT}"
  printf '\n%b維護%b\n' "$(c "1")" "$(reset)"
  printf '  安裝/重載：scripts/install-launchd.sh\n'
  printf '  狀態：    scripts/install-launchd.sh --status\n'
  printf '  下單層：  scripts/install-order-launchd.sh（目前 %s）\n' \
    "$(launchctl list 2>/dev/null | grep -q 'com.jackm4.goldenstocks.order-wake' && echo '已載入' || echo '未安裝')"
  hr
}

case "${1:-}" in
  --json)
    echo '{"error":"--json not implemented yet"}' >&2
    exit 2
    ;;
  -h|--help)
    sed -n '2,6p' "$0"
    exit 0
    ;;
esac

print_header
print_summary
print_alerts
print_job_table
print_today_timeline
print_log_hints
