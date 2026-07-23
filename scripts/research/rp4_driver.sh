#!/bin/bash
# RP-4 driver: chains all sweep runs; resumable (skips outputs that already exist).
# Log: appends to logs/rp4_run.log (repo) so it syncs back to the Mac side.
# Auto-detect project root from this script's location (works on Mac and sandbox).
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" || exit 1
export PYTHONPATH=src
mkdir -p logs
# Prefer repo venv when it runs on this OS (Mac); fall back to system python3 (sandbox).
PY="python3"
if [ -x ".venv/bin/python3" ] && ".venv/bin/python3" -c "import pandas" >/dev/null 2>&1; then
  PY=".venv/bin/python3"
fi
R=reports/research/rrg

run() {
  local out="$1"; shift
  if [ -s "$out" ]; then echo "[rp4] SKIP (exists): $out"; return 0; fi
  echo "[rp4] START $(date '+%F %T') -> $out"
  "$PY" "$@" --out "$out"
  local rc=$?
  echo "[rp4] END   $(date '+%F %T') rc=$rc -> $out"
  # backup copies in /tmp in case of sync weirdness
  cp -f "$out" /tmp/ 2>/dev/null
  cp -f "${out%.json}.md" /tmp/ 2>/dev/null
  return $rc
}

echo "[rp4] driver boot $(date '+%F %T') pid $$"

# 1) full window 2024-01-02 .. 2026-07-01, target 8
run "$R/20260710_rp4_abc_v3_f1_entry_gate_tp_sweep_hold5d.json" \
  scripts/research/run_abc_v3_f1_entry_gate_tp_sweep.py \
  --date-start 2024-01-02 --date-end 2026-07-01 --target 8

run "$R/20260710_rp4_abc_v3_f1_entry_multifactor_tp_hold5d.json" \
  scripts/research/run_abc_v3_f1_entry_multifactor_tp_sweep.py \
  --date-start 2024-01-02 --date-end 2026-07-01 --target 8

# 2) sub-windows for regime-sensitivity reporting (RP-4 sec.6)
run "$R/20260710_rp4_gate_tp_sweep_1y.json" \
  scripts/research/run_abc_v3_f1_entry_gate_tp_sweep.py \
  --date-start 2025-07-02 --date-end 2026-07-01 --target 8

run "$R/20260710_rp4_gate_tp_sweep_6m.json" \
  scripts/research/run_abc_v3_f1_entry_gate_tp_sweep.py \
  --date-start 2026-01-02 --date-end 2026-07-01 --target 8

run "$R/20260710_rp4_multifactor_tp_1y.json" \
  scripts/research/run_abc_v3_f1_entry_multifactor_tp_sweep.py \
  --date-start 2025-07-02 --date-end 2026-07-01 --target 8

run "$R/20260710_rp4_multifactor_tp_6m.json" \
  scripts/research/run_abc_v3_f1_entry_multifactor_tp_sweep.py \
  --date-start 2026-01-02 --date-end 2026-07-01 --target 8

echo "[rp4] ALL DONE $(date '+%F %T')"
