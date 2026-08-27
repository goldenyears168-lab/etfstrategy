#!/usr/bin/env python3
"""ASSIGNED DIMENSION (2026-08-08): eod_flatten=False (live, src/order/
tmf_channel_config.py:51 "poll must not flatten mid-session") vs
eod_flatten=True -- has this ever been re-validated on the CURRENT
final_v1_4_0_pv16_normal_dhwv_block recipe, on all 4 sanctioned windows,
with day-clustered stats?

Prior art (verified by reading): fullnight_eod_gap_lab.py/.json +
fullnight_junjul_restatement.py/.json (topic tmf-fullnight-eod-gap,
graduated 2026-08-05) ran a 4-arm True-vs-False bake-off on 10 then 43
Apr-Jul sessions using the OLD Final v1.1.x recipe (hang 30/60,
far_cover 80/120) -- pre-dates v1.2.0/v1.3.0/v1.4.0. No day-clustered
t-test was run (raw net/WR only). No 4-sanctioned-window check exists.

MECHANISM NOTE (found while building this fork, not previously documented):
the per-day research harness (used by every tx_channel_* lab, including
tonight's baseline) calls simulate() independently per CALENDAR-DATE array
(00:00-23:59), not one continuous multi-day simulation. Each date's array
already contains the tail of the previous evening's night session
(00:00-08:44) and the head of that evening's own night session
(15:00-23:59) -- so "session end" here is an ARBITRARY 23:59 calendar
boundary, not the real night-session close (~05:00 next date). Under
eod_flatten=False (live setting), any position still open at 23:59 is
simply never marked-to-market or realized by summarize() -- causal_engine
returns it as `open_pos` but summarize() only sums closed-trade `pnl`, and
the NEXT date's simulate() call starts flat (state does not carry across
the independent per-day calls). So in this harness (not necessarily in the
live worker, which really is continuous), False does not mean "let it ride
till the real exit" -- it means "silently drop this trade from the P&L
series with no realization, ever." True means "force-close at the 23:59
close price," which is also not the real exit price/time but at least
realizes something. Neither is a perfect proxy for live-continuous
behavior; this script tests the two exactly as the existing harness/
research convention defines them (same convention as fullnight_eod_gap_lab
and every tx_channel_* script), because that convention is what "the
current validated baseline" (mean=+38.3, std=270.3, n=265) was itself
computed with.

THIS SCRIPT: true re-simulation, exact live PAPER_RECIPE (imported from
src/order/tmf_channel_config.py, no hand-copy) with only eod_flatten
flipped True, across all 4 sanctioned windows (w83/julsep25/octdec25/
janmar26, 265 days). Day-clustered paired t-test, per-window breakdown,
single best/worst-day and 2026-06-05 sensitivity, plus a direct count/
characterization of how many day-boundary "dropped" positions exist under
the False baseline and their would-be mark-to-market size.

Observe-only research. Does not touch src/order/, config/order.yaml,
config/strategy.yaml, config/research.yaml, or src/tmf_channel/causal_engine.py.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

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

assert PAPER_RECIPE.get("eod_flatten") is False, (
    "live PAPER_RECIPE eod_flatten drifted from assumed baseline (False) -- re-check"
)


def day_arrays(rows, day):
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
    true_recipe = deepcopy(PAPER_RECIPE)
    true_recipe["eod_flatten"] = True

    day_meta = []
    for cache in CACHE_ORDER:
        for d in list_days(source=cache):
            rows = load_day(d, source=cache)
            if not rows:
                continue
            day_meta.append((d, WINDOW_LABELS[cache], day_arrays(rows, d)))
    print(f"total days across 4 windows: {len(day_meta)}", flush=True)

    base_net = {}
    dropped_open_n = 0
    dropped_open_mtm = []
    for d, w, arrs in day_meta:
        tr, ev, ws, wl, rvol, regime, open_pos = simulate(*arrs, base_recipe, vix_delta=vix)
        s = summarize(tr) if tr else {"net": 0.0}
        base_net[d] = float(s.get("net") or 0.0)
        if open_pos:
            dropped_open_n += 1
            side = open_pos.get("s")
            ep = float(open_pos.get("ep") or 0.0)
            last_c = arrs[3][-1]
            mtm = (last_c - ep) if side == "L" else (ep - last_c)
            dropped_open_mtm.append(mtm)

    pooled = np.array([base_net[d] for d, _, _ in day_meta])
    print("\n=== BASELINE (live PAPER_RECIPE, eod_flatten=False) ===")
    print(f"n={len(pooled)} mean={pooled.mean():.1f} std={pooled.std(ddof=1):.1f} "
          f"sharpe={pooled.mean()/pooled.std(ddof=1):.4f} sum={pooled.sum():.1f}", flush=True)
    print(f"days with a position still open at 23:59 (dropped, uncounted): "
          f"{dropped_open_n}/{len(day_meta)} "
          f"({100*dropped_open_n/len(day_meta):.1f}%)")
    if dropped_open_mtm:
        arr_mtm = np.array(dropped_open_mtm)
        print(f"  their would-be mark-to-market at 23:59 close: mean={arr_mtm.mean():+.1f} "
              f"sum={arr_mtm.sum():+.1f} min={arr_mtm.min():+.1f} max={arr_mtm.max():+.1f}")

    nets_true = {}
    for d, w, arrs in day_meta:
        tr, *_ = simulate(*arrs, true_recipe, vix_delta=vix)
        s = summarize(tr) if tr else {"net": 0.0}
        nets_true[d] = float(s.get("net") or 0.0)
    arr_true = np.array([nets_true[d] for d, _, _ in day_meta])
    print("\n--- eod_flatten=True ---")
    print(f"  pooled n={len(arr_true)} mean={arr_true.mean():.1f} std={arr_true.std(ddof=1):.1f} "
          f"sharpe={arr_true.mean()/arr_true.std(ddof=1):.4f} sum={arr_true.sum():.1f}")

    diffs = arr_true - pooled
    t, p = sstats.ttest_1samp(diffs, 0.0)
    print(f"  paired vs baseline: mean_diff/day={diffs.mean():+.1f} t={t:.2f} p={p:.4f}")
    for w in ("w83", "julsep25", "octdec25", "janmar26"):
        idx = [i for i, (d, ww, _) in enumerate(day_meta) if ww == w]
        dsub = diffs[idx]
        base_sub = pooled[idx]
        true_sub = arr_true[idx]
        t_, p_ = sstats.ttest_1samp(dsub, 0.0)
        print(f"    {w:10s} n={len(dsub):3d} base_mean={base_sub.mean():+7.1f} "
              f"true_mean={true_sub.mean():+7.1f} mean_diff={dsub.mean():+7.1f} "
              f"sum_diff={dsub.sum():+8.1f} t={t_:.2f} p={p_:.4f}")

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
            print(f"  excl 2026-06-05: mean_diff={diffs_excl605.mean():+.1f} t={t3:.2f} "
                  f"p={p3:.4f} sum_diff={diffs_excl605.sum():+.1f}")

    # best single day too, for symmetry
    best_i = int(np.argmax(diffs))
    worst_neg_i = int(np.argmin(diffs))
    print(f"  best-for-True day={day_meta[best_i][0]} diff={diffs[best_i]:+.1f}; "
          f"worst-for-True day={day_meta[worst_neg_i][0]} diff={diffs[worst_neg_i]:+.1f}")


if __name__ == "__main__":
    main()
