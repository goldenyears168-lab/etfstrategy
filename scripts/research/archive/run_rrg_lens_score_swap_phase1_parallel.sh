#!/usr/bin/env bash
# Phase 1 · 依 candidate_gate 平行 sweep（alpha × max_slots）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DATE_START="${DATE_START:-2026-01-01}"
DATE_END="${DATE_END:-2026-06-22}"
OUT_DIR="${OUT_DIR:-reports/research/rrg/lens_score_swap_phase1}"
mkdir -p "$OUT_DIR"

GATES=(lens_only tier2 mono_tier2 mono_fresh_2d leading_improving)
pids=()
for gate in "${GATES[@]}"; do
  python3 scripts/run_rrg_lens_score_swap.py \
    --phase 1 \
    --sweep \
    --gate "$gate" \
    --date-start "$DATE_START" \
    --date-end "$DATE_END" \
    --baseline \
    --out "$OUT_DIR/gate_${gate}.json" \
    --md "$OUT_DIR/gate_${gate}.md" &
  pids+=($!)
done

fail=0
for pid in "${pids[@]}"; do
  if ! wait "$pid"; then
    fail=1
  fi
done

python3 - <<'PY' "$OUT_DIR"
import json
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])
rows = []
for path in sorted(out_dir.glob("gate_*.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    best = data.get("best") or {}
    cfg = best.get("config") or {}
    rows.append(
        {
            "gate": cfg.get("candidate_gate"),
            "alpha": cfg.get("alpha"),
            "max_slots": cfg.get("max_slots"),
            "mean_excess_pct": best.get("mean_excess_pct"),
            "win_rate_vs_bench_pct": best.get("win_rate_vs_bench_pct"),
            "n_periods": best.get("n_periods"),
            "swaps_total": best.get("swaps_total"),
            "kbar_coverage_pct": best.get("kbar_coverage_pct"),
            "source": path.name,
        }
    )
rows.sort(key=lambda r: (-(r.get("mean_excess_pct") or -999), -(r.get("n_periods") or 0)))
payload = {"phase": 1, "by_gate_best": rows, "global_best": rows[0] if rows else None}
merged = out_dir / "phase1_merged.json"
merged.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Wrote {merged}")
if rows:
    g = rows[0]
    print(
        f"Global best: gate={g['gate']} alpha={g['alpha']} slots={g['max_slots']} "
        f"mean_excess={g['mean_excess_pct']}"
    )
PY

exit "$fail"
