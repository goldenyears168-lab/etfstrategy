#!/usr/bin/env python3
"""ASSIGNED DIMENSION (2026-08-08): VIXTWN calib (blend, gamma=5, day-only)
-- is the current live choice actually validated across the 4 sanctioned
windows with day-clustered significance?

Prior art (verified by reading):
  - r_vixtwn_1m_calib_bakeoff.py/.json: only isolated test of the blend
    knob. Old jack_channel_v6_pv/hang_anchor_causal_lab engine (predates
    causal_engine.py). 25-day window (2026-06-26..07-31): blend_g5 wins.
    Secondary 83-day check (2026-04-01..07-31): blend_g8 wins instead,
    blend_g5 trails (+76.8 vs +81.8). No t-test/p-value anywhere in the
    file (grepped: t_stat/p_value/HAC/newey all absent). Frozen block
    explicitly "observe_only": true.
  - r_h2h_v113_vs_specialized.json: promotion evidence for the 16-cell
    book as a WHOLE (BIAS+EARLY+cellbook+VIX blend stacked together) --
    explicitly notes "this is the replace-live question, not a
    single-knob ablation." VIXTWN blend was never isolated here either.
  - Neither covers the 4 sanctioned windows (w83/julsep25/octdec25/
    janmar26) as a single day-clustered test, and neither runs on the
    current causal_engine.py / v1.4.0 recipe (day|normal + day|dhwv now
    fully blocked, which changes which day cells even fire the VIX calib).

THIS SCRIPT: true re-simulation of vixtwn_calib=OFF (mode="none" on every
day cell AND the top-level default, night cells already "none" live) vs
the exact live PAPER_RECIPE (mode="blend", gamma=5.0), across all 4
sanctioned windows -- 265 days, day-clustered paired t-test, per-window
breakdown, single-day and 2026-06-05 sensitivity checks, pooled
mean/std/Sharpe(=mean/std) for both arms.

No engine fork: vixtwn_calib is a pure recipe-dict / per-cell string read
by causal_engine's hang-offset step (vixtwn_hang_adj) -- overriding the
recipe via the frozen public simulate() facade IS the true re-simulation
(methodology rule #2).

Observe-only research. Does not touch src/order/, config/order.yaml,
config/strategy.yaml, config/research.yaml, or src/tmf_channel/causal_engine.py.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import numpy as np
from scipy import stats as sstats

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_1m, load_vixtwn_delta, simulate, summarize

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

assert PAPER_RECIPE.get("vixtwn_calib") == "blend", "live top-level vixtwn_calib drifted"
assert float(PAPER_RECIPE.get("vixtwn_calib_gamma") or 0.0) == 5.0, "live gamma drifted"
for _pv, _cell in PAPER_RECIPE["session_pv_book"]["day"].items():
    assert _cell.get("vixtwn_calib") == "blend", f"day|{_pv} vixtwn_calib drifted"
    assert float(_cell.get("vixtwn_calib_gamma") or 0.0) == 5.0, f"day|{_pv} gamma drifted"
for _pv, _cell in PAPER_RECIPE["session_pv_book"]["night"].items():
    assert (_cell.get("vixtwn_calib") or "none") == "none", f"night|{_pv} vixtwn_calib drifted"


def off_recipe(v1m) -> dict:
    r = deepcopy(PAPER_RECIPE)
    r["vixtwn_calib"] = "none"
    for _pv, _cell in r["session_pv_book"]["day"].items():
        _cell["vixtwn_calib"] = "none"
    r["vixtwn_1m"] = v1m
    return r


def base_recipe_with(v1m) -> dict:
    r = deepcopy(PAPER_RECIPE)
    r["vixtwn_1m"] = v1m  # CRITICAL: live worker injects this at runtime
    # (tmf_channel_order.py:417 run_recipe["vixtwn_1m"] = load_vixtwn_1m_cached());
    # PAPER_RECIPE itself carries vixtwn_1m=None, so calling simulate() on the
    # bare recipe silently makes vixtwn_calib a no-op regardless of mode/gamma.
    return r


def day_arrays(day, rows):
    # NOTE: cache rows carry bare "HH:MM" in "t" (no date). _day(ts) in
    # causal_engine.py does str(ts)[:10] -- WITHOUT the date prefix this
    # silently returns "HH:MM" instead of the calendar day, so every
    # date-keyed lookup (vixtwn_1m / us_vix_1m / nq_on_1m / gap_bias_by_day
    # / day_dir_map) becomes a permanent no-op with zero error. Must
    # reconstruct the full ISO timestamp exactly like the live day_arrays()
    # helper (r_strict_paper_bias_overlay.py:71-79) does.
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r['t']}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def net_for(recipe, day_meta, vix):
    nets = {}
    for d, w, arrs in day_meta:
        tr, *_ = simulate(*arrs, recipe, vix_delta=vix)
        s = summarize(tr) if tr else {"net": 0.0}
        nets[d] = float(s.get("net") or 0.0)
    return nets


def report(label, arr):
    print(f"  {label}: n={len(arr)} mean={arr.mean():+.1f} std={arr.std(ddof=1):.1f} "
          f"sharpe={arr.mean()/arr.std(ddof=1):.4f} sum={arr.sum():+.1f} "
          f"win_rate={100*np.mean(arr>0):.1f}%")


def main():
    vix = load_vixtwn_delta() or {}
    v1m = load_vixtwn_1m() or {}
    print(f"vixtwn_1m coverage: {len(v1m)} days, "
          f"{min(v1m) if v1m else None}..{max(v1m) if v1m else None}", flush=True)

    day_meta = []
    for cache in CACHE_ORDER:
        for d in list_days(source=cache):
            rows = load_day(d, source=cache)
            if not rows:
                continue
            day_meta.append((d, WINDOW_LABELS[cache], day_arrays(d, rows)))
    print(f"total days across 4 windows: {len(day_meta)}", flush=True)

    cov = {}
    for d, w, _ in day_meta:
        cov.setdefault(w, [0, 0])
        cov[w][1] += 1
        if d in v1m:
            cov[w][0] += 1
    print("vixtwn_1m coverage per window (days_with_data / total):")
    for w, (has, tot) in cov.items():
        print(f"  {w:10s} {has}/{tot}")

    base_net = net_for(base_recipe_with(v1m), day_meta, vix)
    off_net = net_for(off_recipe(v1m), day_meta, vix)

    base_arr = np.array([base_net[d] for d, _, _ in day_meta])
    off_arr = np.array([off_net[d] for d, _, _ in day_meta])
    diffs = off_arr - base_arr

    print("\n=== POOLED (265 days) ===")
    report("BASELINE (blend g=5, live)", base_arr)
    report("OFF (vixtwn_calib=none)   ", off_arr)
    t, p = sstats.ttest_1samp(diffs, 0.0)
    print(f"  paired diff (OFF - BASE): mean={diffs.mean():+.2f} sum={diffs.sum():+.1f} t={t:.3f} p={p:.4f}")

    print("\n=== PER WINDOW ===")
    for w in ("w83", "julsep25", "octdec25", "janmar26"):
        idx = [i for i, (d, ww, _) in enumerate(day_meta) if ww == w]
        b = base_arr[idx]
        o = off_arr[idx]
        dsub = diffs[idx]
        t_, p_ = sstats.ttest_1samp(dsub, 0.0)
        print(f"  {w:10s} n={len(idx):3d}  base_mean={b.mean():+8.1f} off_mean={o.mean():+8.1f} "
              f"diff_mean={dsub.mean():+7.1f} diff_sum={dsub.sum():+8.1f} t={t_:.2f} p={p_:.4f}")

    order_ = sorted(range(len(diffs)), key=lambda i: -abs(diffs[i]))
    worst_i = order_[0]
    worst_day = day_meta[worst_i][0]
    diffs_excl = np.delete(diffs, worst_i)
    t2, p2 = sstats.ttest_1samp(diffs_excl, 0.0)
    print(f"\nlargest|diff| day={worst_day} diff={diffs[worst_i]:+.1f}; "
          f"excl it: mean_diff={diffs_excl.mean():+.2f} t={t2:.2f} p={p2:.4f} sum_diff={diffs_excl.sum():+.1f}")

    days_only = [d for d, _, _ in day_meta]
    if "2026-06-05" in days_only:
        idx605 = days_only.index("2026-06-05")
        diffs_excl605 = np.delete(diffs, idx605)
        t3, p3 = sstats.ttest_1samp(diffs_excl605, 0.0)
        print(f"excl 2026-06-05: mean_diff={diffs_excl605.mean():+.2f} t={t3:.2f} p={p3:.4f} sum_diff={diffs_excl605.sum():+.1f}")

    # best day sensitivity too (single best day dominance check)
    best_i = int(np.argmax(base_arr))
    print(f"\nbaseline single best day={day_meta[best_i][0]} net={base_arr[best_i]:+.1f} "
          f"(of pooled sum {base_arr.sum():+.1f})")
    base_excl_best = np.delete(base_arr, best_i)
    print(f"  baseline excl best day: mean={base_excl_best.mean():+.2f} std={base_excl_best.std(ddof=1):.1f} "
          f"sharpe={base_excl_best.mean()/base_excl_best.std(ddof=1):.4f}")


if __name__ == "__main__":
    main()
