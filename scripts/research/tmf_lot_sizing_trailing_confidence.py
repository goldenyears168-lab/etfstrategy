#!/usr/bin/env python3
"""ASSIGNED DIMENSION (2026-08-08): fixed 1-lot sizing vs dynamic (vol/
confidence) sizing, audited against the LIVE v1.4.0 PAPER_RECIPE.

Design under test: "trailing realized-edge stand-aside sizing." TMF micro is
1-tick-lot granularity (max_lots=1 live) so there is no room to size UP
without violating the already-settled H-LOT-FIXED2 finding (larger uniform
exposure monotonically worsens negative expectancy at tick granularity).
The only sizing lever left within a 1-lot instrument is DOWN: 1 lot vs 0
(stand aside). This tests a confidence signal built from the strategy's OWN
trailing realized daily P&L (a common equity-curve / recent-Sharpe overlay),
NOT the amp+volume forecast (that forecast is bar-level/intraday and was
already shown, in prior campaigns, not to survive being wired into per-trade
entry/exit decisions on this engine -- reusing it for day-level sizing would
just be a coarser repeat of the same failure mode). This design differs from
those prior (all per-trade / per-bar) attempts: it is a SESSION-level,
pre-open decision using only PRIOR days' own realized results -- never tried
before per the audit.

Mechanism (causal, no look-ahead):
  1. Run baseline (unmodified PAPER_RECIPE, max_lots=1) once per day,
     chronologically, across all 4 sanctioned windows concatenated in true
     calendar order (julsep25 -> octdec25 -> janmar26 -> w83, verified via
     cache_store.list_days date ranges). This reproduces the given baseline
     (mean=+38.3, std=270.3, n=265) as a sanity check.
  2. trailing_mean[d] = mean(baseline_net[d-N:d]) over the N days STRICTLY
     BEFORE day d (a real trailing/rolling window, not expanding, not
     including today -- methodology rule #1). First N days of the whole
     265-day history (chronological start of julsep25) have no trailing
     history -> trade at baseline size (can't have a stand-aside opinion
     before any track record exists).
  3. Sizing rule: if trailing_mean[d] > 0, trade normally (recipe unchanged,
     net_dyn[d] = net_base[d]). If trailing_mean[d] <= 0, stand aside for the
     WHOLE day: recipe is TRUE re-simulated (engine.simulate call, not a
     post-hoc filter) with max_lots=0, which causes every open_lot() call to
     reject (len(lots) >= p["max_lots"] with max_lots=0 is always true) ->
     net_dyn[d] is the actual simulated flat-day net (should be 0, verified
     below, no held-over state exists since each day is simulated
     independently from the cache).
  4. Reported for N in {10, 15, 20} trading days for robustness; N=15 is the
     primary (a common ~3-week lookback), not cherry-picked post-hoc from
     the 3.
"""
from __future__ import annotations

from copy import deepcopy

import numpy as np
from scipy import stats as sstats

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate, summarize

CACHE_ORDER = [
    "tx_1m_julsep_holdout_cache.json",
    "tx_1m_octdec_holdout_cache.json",
    "tx_1m_janmar_holdout_cache.json",
    "tx_1m_fullnight_cache_full.json",
]
WINDOW_LABELS = {
    "tx_1m_julsep_holdout_cache.json": "julsep25",
    "tx_1m_octdec_holdout_cache.json": "octdec25",
    "tx_1m_janmar_holdout_cache.json": "janmar26",
    "tx_1m_fullnight_cache_full.json": "w83",
}


def day_arrays(rows, day):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def sharpe(mean, std):
    return mean / std if std else float("nan")


def main():
    vix = load_vixtwn_delta() or {}
    base_recipe = deepcopy(PAPER_RECIPE)
    base_recipe.setdefault("hang_anchor", "O")

    stand_aside_recipe = deepcopy(base_recipe)
    stand_aside_recipe["max_lots"] = 0

    all_days = []
    day_meta = {}
    for cache in CACHE_ORDER:
        for d in list_days(source=cache):
            rows = load_day(d, source=cache)
            if not rows:
                continue
            O, H, L, C, V, T = day_arrays(rows, d)
            day_meta[d] = dict(cache=cache, O=O, H=H, L=L, C=C, V=V, T=T,
                                window=WINDOW_LABELS[cache])
            all_days.append(d)
    all_days = sorted(set(all_days))
    print(f"total days across 4 windows (chronological): {len(all_days)}")

    # Step 1: baseline net per day (single source of truth, reused for all N).
    base_net = {}
    for d in all_days:
        m = day_meta[d]
        tr, *_ = simulate(m["O"], m["H"], m["L"], m["C"], m["V"], m["T"], base_recipe, vix_delta=vix)
        base_net[d] = float((summarize(tr) if tr else {"net": 0.0}).get("net") or 0.0)

    pooled = np.array([base_net[d] for d in all_days])
    print(f"\n=== baseline sanity check (should match given: mean=+38.3 std=270.3 n=265) ===")
    print(f"n={len(pooled)} mean={pooled.mean():.1f} std={pooled.std(ddof=1):.1f} sum={pooled.sum():.1f} "
          f"win_rate={(pooled>0).mean()*100:.1f}%")

    for N in (10, 15, 20):
        rows_out = []
        for i, d in enumerate(all_days):
            m = day_meta[d]
            if i < N:
                net_dyn = base_net[d]
                traded = True
            else:
                trailing = [base_net[all_days[j]] for j in range(i - N, i)]
                tmean = float(np.mean(trailing))
                if tmean > 0:
                    net_dyn = base_net[d]
                    traded = True
                else:
                    tr, *_ = simulate(m["O"], m["H"], m["L"], m["C"], m["V"], m["T"],
                                       stand_aside_recipe, vix_delta=vix)
                    net_dyn = float((summarize(tr) if tr else {"net": 0.0}).get("net") or 0.0)
                    traded = False
            rows_out.append(dict(day=d, window=m["window"], net_base=base_net[d],
                                  net_dyn=net_dyn, traded=traded))

        stood_aside = sum(1 for r in rows_out if not r["traded"])
        nonzero_stand_aside_net = [r["net_dyn"] for r in rows_out if not r["traded"] and r["net_dyn"] != 0.0]
        print(f"\n{'='*70}\nN={N} trading-day trailing window")
        print(f"stood-aside days: {stood_aside}/{len(rows_out)} "
              f"(non-zero net while stood aside: {len(nonzero_stand_aside_net)}"
              f"{' -> ' + str(nonzero_stand_aside_net[:5]) if nonzero_stand_aside_net else ''})")

        base_arr = np.array([r["net_base"] for r in rows_out])
        dyn_arr = np.array([r["net_dyn"] for r in rows_out])
        diffs = dyn_arr - base_arr
        t, p = sstats.ttest_1samp(diffs, 0.0)
        print(f"\n--- pooled (n={len(rows_out)}) ---")
        print(f"BASELINE: mean={base_arr.mean():.1f} std={base_arr.std(ddof=1):.1f} "
              f"sharpe={sharpe(base_arr.mean(), base_arr.std(ddof=1)):.3f} sum={base_arr.sum():.1f} "
              f"win_rate={(base_arr>0).mean()*100:.1f}%")
        print(f"DYNAMIC : mean={dyn_arr.mean():.1f} std={dyn_arr.std(ddof=1):.1f} "
              f"sharpe={sharpe(dyn_arr.mean(), dyn_arr.std(ddof=1)):.3f} sum={dyn_arr.sum():.1f} "
              f"win_rate={(dyn_arr>0).mean()*100:.1f}%")
        print(f"paired diff: mean_diff/day={diffs.mean():.1f} t={t:.2f} p={p:.4f}")

        print("--- per window ---")
        for w in ("julsep25", "octdec25", "janmar26", "w83"):
            sub = [r for r in rows_out if r["window"] == w]
            if len(sub) < 3:
                continue
            b = np.array([r["net_base"] for r in sub])
            dd = np.array([r["net_dyn"] for r in sub])
            diff = dd - b
            t_, p_ = sstats.ttest_1samp(diff, 0.0)
            n_stood = sum(1 for r in sub if not r["traded"])
            print(f"  {w:10s} n={len(sub):3d} stood_aside={n_stood:3d} "
                  f"base_mean={b.mean():7.1f} base_std={b.std(ddof=1):7.1f} "
                  f"dyn_mean={dd.mean():7.1f} dyn_std={dd.std(ddof=1):7.1f} "
                  f"diff_mean={diff.mean():7.1f} t={t_:.2f} p={p_:.4f}")

        # single-day sensitivity
        day_diff = sorted(((r["day"], r["net_dyn"] - r["net_base"]) for r in rows_out), key=lambda x: -abs(x[1]))
        print("largest |diff| days:", day_diff[:3])
        if len(diffs) > 1:
            worst_idx = int(np.argmax(np.abs(diffs)))
            diffs_excl = np.delete(diffs, worst_idx)
            dyn_excl = np.delete(dyn_arr, worst_idx)
            print(f"excl single largest-|diff| day ({rows_out[worst_idx]['day']}, diff={diffs[worst_idx]:.1f}): "
                  f"dyn_mean={dyn_excl.mean():.1f} dyn_std={dyn_excl.std(ddof=1):.1f} "
                  f"sharpe={sharpe(dyn_excl.mean(), dyn_excl.std(ddof=1)):.3f} mean_diff_excl={diffs_excl.mean():.1f}")
        if any(d == "2026-06-05" for d, _ in day_diff):
            diffs_excl2 = np.array([r["net_dyn"] - r["net_base"] for r in rows_out if r["day"] != "2026-06-05"])
            print(f"excl 2026-06-05: mean_diff={diffs_excl2.mean():.1f} n={len(diffs_excl2)}")


if __name__ == "__main__":
    main()
