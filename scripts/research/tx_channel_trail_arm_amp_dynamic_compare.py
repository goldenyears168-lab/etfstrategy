#!/usr/bin/env python3
"""ASSIGNED ITEM #2 (2026-08-08): does replacing the FIXED trail_arm_pts=50
threshold with a value DERIVED FROM an amp+volume forecast signal (wider arm
when predicted near-term amplitude/volume elevated, tighter when quiet) beat
current PAPER_RECIPE (v1.4.0, fixed trail_arm_pts=50), via TRUE re-simulation
(engine fork, not post-hoc trade editing) on w83 + 3 holdout windows
(julsep25, octdec25, janmar26 -> chronologically contiguous with w83)?

Prior art read first: struct_gated_trail_arm_compare.py + its 3 verify-fold
JSONs tested a DIFFERENT idea (gate struct_break behind trail-arm having
fired at all) on an OLDER engine (jack_channel_v6_pv fork) — mixed result,
not adopted (design window strong, fold2 modest, fold3 lost to simply
disabling struct entirely). That study is NOT re-derived here; it targeted
struct_break gating, this targets the trail EXIT's own arm threshold width.

Design (causal, no look-ahead):
  1. Per calendar day (from cache_store.load_day), compute
     weighted_amp30(H,L) and vol_level30(V) — same triangular-recency-
     weighted 30-bar formulas validated tonight in
     tx_channel_amp_volume_interaction.py / tx_channel_amp_persistence_
     drivers2.py — over that day's own bar sequence (resets each day,
     matching the array cache_store hands the engine).
  2. Percentile-rank each bar's (wamp, vlvl) against a TRAILING 30-CALENDAR-
     DAY pooled history of bars from all PRIOR days (chronological across
     all 4 caches; methodology rule #1 — never a global/full-sample
     threshold). combo = 0.7*pctile(wamp) + 0.3*pctile(vlvl).
  3. arm_dyn[t] = clip(20 + 80*combo[t], 20, 100) (vs fixed 50). Days before
     30-day warmup use the engine's normal fixed trail_arm_pts=50 fallback
     (trail_arm_dyn_series[t] is None until warmup satisfied).
  4. Frozen AT ENTRY per lot (tmf_channel_trail_arm_amp_dynamic_engine.py's
     open_lot stashes arm_dyn on the lot dict; both trail-check sites read
     it in preference to the fixed scalar) — arm width does not float
     mid-trade, matching "predicted near-term amplitude AT ENTRY" framing.
     giveback stays fixed at 40 (only arm width is dynamic).

Engine: research fork (byte-identical to tmf_channel.causal_engine.simulate
except: (a) trail_arm_dyn_series lookup, (b) lots[] carries arm_dyn). PAPER_
RECIPE has tick_native=False, so this only exercises the bar-close exit path
(same path harness.run_days exercises) — no tick-level claim is made here.

Day-clustered paired t-test (dynamic_net - baseline_net) per day, only over
days with sufficient 30-day warmup (both windows). Explicit single-day
sensitivity check (drop 2026-06-05, drop best/worst day).
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
from tmf_channel.engine import load_vixtwn_delta, summarize

import tx_channel_trail_arm_amp_dynamic_engine as engine  # noqa: E402

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
AMP_WIN = 30
WARMUP_DAYS = 30


def weighted_amp30(H, L):
    n = len(H)
    w = np.arange(1, AMP_WIN + 1, dtype=float)
    wsum = w.sum()
    rng = np.array(H) - np.array(L)
    out = np.full(n, np.nan)
    for i in range(AMP_WIN - 1, n):
        out[i] = (rng[i - AMP_WIN + 1 : i + 1] * w).sum() / wsum
    return out


def vol_level30(V):
    n = len(V)
    v = np.array([float(x or 0) for x in V])
    out = np.full(n, np.nan)
    for i in range(AMP_WIN - 1, n):
        out[i] = v[i - AMP_WIN + 1 : i + 1].mean()
    return out


def pctile_rank(x: float, hist: np.ndarray) -> float:
    if hist.size == 0 or np.isnan(x):
        return 0.5
    return float((hist < x).sum()) / hist.size


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
    all_days = []
    day_meta = {}  # day -> (cache, rows, wamp, vlvl, window_label)
    for cache in CACHE_ORDER:
        for d in list_days(source=cache):
            rows = load_day(d, source=cache)
            if not rows:
                continue
            O, H, L, C, V, T = day_arrays(d, rows)
            wamp = weighted_amp30(H, L)
            vlvl = vol_level30(V)
            day_meta[d] = dict(
                cache=cache, rows=rows, O=O, H=H, L=L, C=C, V=V, T=T,
                wamp=wamp, vlvl=vlvl, window=WINDOW_LABELS[cache],
            )
            all_days.append(d)
    all_days = sorted(set(all_days))
    print(f"total days across 4 windows: {len(all_days)}")

    from collections import deque
    TRAIL_DAYS = 30  # true trailing window (not expanding) — methodology rule #1
    hist_deque: deque = deque(maxlen=TRAIL_DAYS)  # each entry: (wamp_valid, vlvl_valid) arrays for one day

    base_recipe = deepcopy(PAPER_RECIPE)
    base_recipe.setdefault("hang_anchor", "O")

    rows_out = []
    for day in all_days:
        m = day_meta[day]
        warm_ok = len(hist_deque) >= WARMUP_DAYS

        arm_series = None
        if warm_ok:
            wh = np.concatenate([x[0] for x in hist_deque])
            vh = np.concatenate([x[1] for x in hist_deque])
            n = len(m["wamp"])
            arm_series = [None] * n
            for i in range(n):
                w_i, v_i = m["wamp"][i], m["vlvl"][i]
                if np.isnan(w_i) or np.isnan(v_i):
                    continue
                combo = 0.7 * pctile_rank(w_i, wh) + 0.3 * pctile_rank(v_i, vh)
                arm_series[i] = float(np.clip(20.0 + 80.0 * combo, 20.0, 100.0))

        # baseline (fixed trail_arm_pts=50, exact PAPER_RECIPE)
        tr_base, *_ = engine.simulate(m["O"], m["H"], m["L"], m["C"], m["V"], m["T"], base_recipe, vix_delta=vix)
        s_base = summarize(tr_base) if tr_base else {"net": 0.0}

        # dynamic (only differs once warmup satisfied; else identical net)
        if warm_ok:
            dyn_recipe = deepcopy(base_recipe)
            dyn_recipe["trail_arm_dyn_series"] = arm_series
            tr_dyn, *_ = engine.simulate(m["O"], m["H"], m["L"], m["C"], m["V"], m["T"], dyn_recipe, vix_delta=vix)
            s_dyn = summarize(tr_dyn) if tr_dyn else {"net": 0.0}
        else:
            tr_dyn, s_dyn = tr_base, s_base

        rows_out.append(dict(
            day=day, window=m["window"], warm=warm_ok,
            net_base=float(s_base.get("net") or 0.0),
            net_dyn=float(s_dyn.get("net") or 0.0),
            arm_mean=(float(np.nanmean([a for a in arm_series if a is not None])) if (warm_ok and any(a is not None for a in arm_series)) else None),
        ))

        # push today's bars into trailing-30-day deque AFTER scoring (causal, true rolling)
        valid = ~(np.isnan(m["wamp"]) | np.isnan(m["vlvl"]))
        hist_deque.append((m["wamp"][valid], m["vlvl"][valid]))

    scored = [r for r in rows_out if r["warm"]]
    print(f"scored (post-warmup) days: {len(scored)} / {len(rows_out)}")

    diffs = np.array([r["net_dyn"] - r["net_base"] for r in scored])
    net_base_tot = sum(r["net_base"] for r in scored)
    net_dyn_tot = sum(r["net_dyn"] for r in scored)
    t, p = sstats.ttest_1samp(diffs, 0.0)
    print(f"\n=== ALL scored days (n={len(scored)}) ===")
    print(f"net_base={net_base_tot:.1f}  net_dyn={net_dyn_tot:.1f}  mean_diff/day={diffs.mean():.1f}  t={t:.2f} p={p:.4f}")

    print("\n--- per window ---")
    for w in ("julsep25", "octdec25", "janmar26", "w83"):
        sub = [r for r in scored if r["window"] == w]
        if len(sub) < 3:
            print(f"  {w:10s} n={len(sub)} (too few)")
            continue
        d = np.array([r["net_dyn"] - r["net_base"] for r in sub])
        t_, p_ = sstats.ttest_1samp(d, 0.0)
        print(f"  {w:10s} n={len(sub):3d} net_base={sum(r['net_base'] for r in sub):9.1f} net_dyn={sum(r['net_dyn'] for r in sub):9.1f} mean_diff={d.mean():7.1f} t={t_:.2f} p={p_:.4f}")

    print("\n--- single-day sensitivity ---")
    day_diff = sorted(((r["day"], r["net_dyn"] - r["net_base"]) for r in scored), key=lambda x: -abs(x[1]))
    print("largest |diff| days:", day_diff[:5])
    if any(d == "2026-06-05" for d, _ in day_diff):
        diffs_excl = np.array([r["net_dyn"] - r["net_base"] for r in scored if r["day"] != "2026-06-05"])
        t2, p2 = sstats.ttest_1samp(diffs_excl, 0.0)
        print(f"excl 2026-06-05: n={len(diffs_excl)} mean_diff={diffs_excl.mean():.1f} t={t2:.2f} p={p2:.4f} sum_diff={diffs_excl.sum():.1f}")
    # drop single best/worst day
    if len(diffs) > 1:
        worst_idx = int(np.argmax(np.abs(diffs)))
        diffs_excl_worst = np.delete(diffs, worst_idx)
        t3, p3 = sstats.ttest_1samp(diffs_excl_worst, 0.0)
        print(f"excl single largest-|diff| day ({scored[worst_idx]['day']}, diff={diffs[worst_idx]:.1f}): "
              f"n={len(diffs_excl_worst)} mean_diff={diffs_excl_worst.mean():.1f} t={t3:.2f} p={p3:.4f} sum_diff={diffs_excl_worst.sum():.1f}")

    arm_means = [r["arm_mean"] for r in scored if r["arm_mean"] is not None]
    print(f"\narm_dyn mean across scored days: {np.mean(arm_means):.1f} (vs fixed 50)  min_day_mean={np.min(arm_means):.1f} max_day_mean={np.max(arm_means):.1f}")


if __name__ == "__main__":
    main()
