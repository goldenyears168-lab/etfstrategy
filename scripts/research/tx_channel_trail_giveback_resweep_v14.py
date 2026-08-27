#!/usr/bin/env python3
"""ASSIGNED DIMENSION (2026-08-08): Trail exit params trail_arm_pts=50 /
trail_giveback_pts=40 -- is there an independent sweep against the CURRENT
live recipe?

Prior art (verified by reading): tmf_realrecipe_exit_sweep_lab.py +
tmf_realrecipe_exit_peek_check_lab.py + tmf_realrecipe_exit_5window_lab.py
(2026-08-05) ran the full IS -> peek -> 3-blind-fold protocol for both
trail_arm_pts in/around {25,35,50,65,80} and trail_giveback_pts in
{20,30,40,50,60}, against baseline=REAL_RECIPE = live PAPER_RECIPE at that
time (Final v1.1.2/v1.1.3, hang_hi=38). trail_arm_pts=35 reversed on 2/3
blind folds (fold1 -121, fold3 -236). trail_giveback_pts in {20,30} showed a
classic monotonic-IS overfit shape that reversed on the very first OOS peek
(giveback=20: peek delta -467; giveback=30: peek delta -1078) -- rejected
before reaching blind folds. Baseline (arm=50, giveback=40) survived both
dimensions as the best tested value.

GAP: that sweep predates final_v1_2_0 (engine promote), final_v1_3_0
(12-cell PV16 celltune v2), and final_v1_4_0 (day|normal + day|div_hh_weak_vol
full block) -- no channel_lab file dated 2026-08-06 or later touches either
param. The cell-tune + dhwv_block changes materially reweighted which PV16
regime cells actually generate trades, so whether giveback=40 is still
optimal for the CURRENT trade mix is unrevalidated, not settled.

THIS SCRIPT: true re-simulation of trail_giveback_pts in {25, 30, 35, 40
(baseline), 50, 60} -- keep trail_arm_pts FIXED at the live value (50; the
arm dimension already has a clean 2-blind-fold rejection at 35 and is not
retested here) -- against the EXACT current live PAPER_RECIPE (imported
live from src/order/tmf_channel_config.py, no hand-copy), across all 4
sanctioned windows (w83/julsep25/octdec25/janmar26, 265 days total). No
engine fork needed: trail_giveback_pts is a pure recipe-dict scalar read by
tmf_channel.causal_engine's exit check (both tick_native and bar-close
paths) -- calling the frozen public simulate() API with an overridden
recipe IS the true re-simulation (methodology rule #2; no post-hoc trade
filtering).

Day-clustered paired t-test (variant_day_net - baseline_day_net), per
window and pooled, plus pooled mean/std/Sharatio(=mean/std) for direct
comparison against tonight's baseline (mean=+38.3, std=270.3, n=265).
Single best/worst day and 2026-06-05 sensitivity checks included.

Observe-only research. Does not touch src/order/, config/order.yaml,
config/strategy.yaml, config/research.yaml, or src/tmf_channel/causal_engine.py.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from scipy import stats as sstats

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate, summarize

CACHE_ORDER = [
    "tx_1m_fullnight_cache_full.json",
    "tx_1m_julsep_holdout_cache.json",
    "tx_1m_octdec_holdout_cache.json",
    "tx_1m_janmar_holdout_cache.json",
]
WINDOW_LABELS = {
    "tx_1m_fullnight_cache_full.json": "w83",
    "tx_1m_julsep_holdout_cache.json": "julsep25",
    "tx_1m_octdec_holdout_cache.json": "octdec25",
    "tx_1m_janmar_holdout_cache.json": "janmar26",
}

GIVEBACK_VALUES = [25.0, 30.0, 35.0, 40.0, 50.0, 60.0]
BASELINE_GIVEBACK = 40.0
assert float(PAPER_RECIPE.get("trail_giveback_pts") or 0.0) == BASELINE_GIVEBACK, (
    "live PAPER_RECIPE trail_giveback_pts drifted from assumed baseline -- re-check"
)
assert float(PAPER_RECIPE.get("trail_arm_pts") or 0.0) == 50.0, "live trail_arm_pts drifted"


def day_arrays(day, rows):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def main():
    vix = load_vixtwn_delta() or {}
    base_recipe = deepcopy(PAPER_RECIPE)

    day_meta = []
    for cache in CACHE_ORDER:
        for d in list_days(source=cache):
            rows = load_day(d, source=cache)
            if not rows:
                continue
            day_meta.append((d, WINDOW_LABELS[cache], day_arrays(d, rows)))
    print(f"total days across 4 windows: {len(day_meta)}", flush=True)

    # baseline nets (computed once, reused for every variant's delta/t-test)
    base_net = {}
    for d, w, arrs in day_meta:
        tr, *_ = simulate(*arrs, base_recipe, vix_delta=vix)
        s = summarize(tr) if tr else {"net": 0.0}
        base_net[d] = float(s.get("net") or 0.0)

    pooled = np.array([base_net[d] for d, _, _ in day_meta])
    print(f"\n=== BASELINE (live PAPER_RECIPE, giveback=40) ===")
    print(f"n={len(pooled)} mean={pooled.mean():.1f} std={pooled.std(ddof=1):.1f} "
          f"sharpe={pooled.mean()/pooled.std(ddof=1):.4f} sum={pooled.sum():.1f}", flush=True)

    results = {}
    for gv in GIVEBACK_VALUES:
        recipe = deepcopy(base_recipe)
        recipe["trail_giveback_pts"] = gv
        nets = {}
        for d, w, arrs in day_meta:
            tr, *_ = simulate(*arrs, recipe, vix_delta=vix)
            s = summarize(tr) if tr else {"net": 0.0}
            nets[d] = float(s.get("net") or 0.0)
        results[gv] = nets
        arr = np.array([nets[d] for d, _, _ in day_meta])
        tag = " <- baseline" if gv == BASELINE_GIVEBACK else ""
        print(f"\n--- giveback={gv:.0f} ---")
        print(f"  pooled n={len(arr)} mean={arr.mean():.1f} std={arr.std(ddof=1):.1f} "
              f"sharpe={arr.mean()/arr.std(ddof=1):.4f} sum={arr.sum():.1f}{tag}")
        if gv != BASELINE_GIVEBACK:
            diffs = arr - pooled
            t, p = sstats.ttest_1samp(diffs, 0.0)
            print(f"  paired vs baseline: mean_diff/day={diffs.mean():+.1f} t={t:.2f} p={p:.4f}")
            for w in ("w83", "julsep25", "octdec25", "janmar26"):
                idx = [i for i, (d, ww, _) in enumerate(day_meta) if ww == w]
                dsub = diffs[idx]
                t_, p_ = sstats.ttest_1samp(dsub, 0.0)
                print(f"    {w:10s} n={len(dsub):3d} mean_diff={dsub.mean():+7.1f} "
                      f"sum_diff={dsub.sum():+8.1f} t={t_:.2f} p={p_:.4f}")
            # single-day sensitivity
            order_ = sorted(range(len(diffs)), key=lambda i: -abs(diffs[i]))
            worst_i = order_[0]
            worst_day = day_meta[worst_i][0]
            diffs_excl = np.delete(diffs, worst_i)
            t2, p2 = sstats.ttest_1samp(diffs_excl, 0.0)
            print(f"  largest|diff| day={worst_day} diff={diffs[worst_i]:+.1f}; "
                  f"excl it: mean_diff={diffs_excl.mean():+.1f} t={t2:.2f} p={p2:.4f} sum_diff={diffs_excl.sum():+.1f}")
            if "2026-06-05" in [d for d, _, _ in day_meta]:
                idx605 = [i for i, (d, _, _) in enumerate(day_meta) if d == "2026-06-05"]
                if idx605:
                    diffs_excl605 = np.delete(diffs, idx605[0])
                    t3, p3 = sstats.ttest_1samp(diffs_excl605, 0.0)
                    print(f"  excl 2026-06-05: mean_diff={diffs_excl605.mean():+.1f} t={t3:.2f} p={p3:.4f} sum_diff={diffs_excl605.sum():+.1f}")


if __name__ == "__main__":
    main()
