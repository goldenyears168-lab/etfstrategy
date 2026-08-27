#!/usr/bin/env python3
"""User-requested (2026-08-08) simplification check: does stripping
CELL_TUNE_V2_PATCHES (12 patches) and vixtwn_calib="blend" back to the
v1.2.0 baseline (SPECIALIZED_PATCHES only, vixtwn_calib="none") lose,
gain, or wash vs the current LIVE recipe (final_v1_4_0_pv16_safety_hardening)?

Trigger: r_gate_anchor_v4_audit.json (2026-08-08) showed CELL_TUNE_V2's
original "5/5 HAC-significant" justification does not survive a corrected,
non-look-ahead NQ gate anchor (0/5 significant, w83 and w25 point estimates
flip negative, 3-quarter pooled OOS p=0.72). User's reaction: rules added
without surviving edge should come back out. This script is the "does the
simplified version actually perform worse, better, or the same" check
before touching src/order/ live code.

True re-simulation via tmf_channel.engine.simulate() on both recipes,
across all 4 sanctioned windows, day-clustered paired comparison.

Does NOT touch src/order/, config/order.yaml, .env, launchd/, scripts/order/.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import freeze_cell_book, SPECIALIZED_PATCHES  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402

SOURCES = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}


def v120_baseline_book() -> dict:
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    return book


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

    live_recipe = dict(PAPER_RECIPE)
    live_recipe["session_pv_book"] = deepcopy(PAPER_RECIPE["session_pv_book"])
    live_recipe.setdefault("hang_anchor", "O")

    simple_recipe = dict(PAPER_RECIPE)
    simple_recipe["session_pv_book"] = v120_baseline_book()
    simple_recipe["vixtwn_calib"] = "none"
    simple_recipe.setdefault("hang_anchor", "O")

    per_window = {}
    all_diffs = []  # (window, day, live_pnl, simple_pnl, diff)

    for label, cache_name in SOURCES.items():
        days = list_days(source=cache_name)
        day_diffs = []
        live_daily = []
        simple_daily = []
        for day in days:
            rows = load_day(day, source=cache_name)
            if not rows:
                continue
            O, H, L, C, V, T = day_arrays(day, rows)

            trades_live, *_ = simulate(O, H, L, C, V, T, live_recipe, vix_delta=vix)
            trades_simple, *_ = simulate(O, H, L, C, V, T, simple_recipe, vix_delta=vix)

            live_pnl = sum(t["pnl"] for t in trades_live)
            simple_pnl = sum(t["pnl"] for t in trades_simple)
            diff = simple_pnl - live_pnl

            live_daily.append(live_pnl)
            simple_daily.append(simple_pnl)
            day_diffs.append(diff)
            all_diffs.append((label, day, live_pnl, simple_pnl, diff))

        n = len(day_diffs)
        mean_diff = st.mean(day_diffs) if n else 0.0
        std_diff = st.stdev(day_diffs) if n > 1 else 0.0
        t_stat = (mean_diff / (std_diff / (n ** 0.5))) if (n > 1 and std_diff > 0) else 0.0
        # two-sided p via normal approx (n large enough across windows; also report raw t)
        try:
            from scipy import stats as sp_stats

            p_val = float(2 * (1 - sp_stats.t.cdf(abs(t_stat), df=n - 1))) if n > 1 else None
        except Exception:
            p_val = None

        per_window[label] = {
            "n_days": n,
            "live_mean": round(st.mean(live_daily), 2) if live_daily else None,
            "live_std": round(st.stdev(live_daily), 2) if len(live_daily) > 1 else None,
            "simple_mean": round(st.mean(simple_daily), 2) if simple_daily else None,
            "simple_std": round(st.stdev(simple_daily), 2) if len(simple_daily) > 1 else None,
            "mean_diff_simple_minus_live": round(mean_diff, 2),
            "t_stat": round(t_stat, 3),
            "p_value": round(p_val, 4) if p_val is not None else None,
        }

    # pooled across all 4 windows
    pooled_diffs = [d for _, _, _, _, d in all_diffs]
    pooled_live = [lp for _, _, lp, _, _ in all_diffs]
    pooled_simple = [sp for _, _, _, sp, _ in all_diffs]
    n_pool = len(pooled_diffs)
    mean_pool = st.mean(pooled_diffs)
    std_pool = st.stdev(pooled_diffs) if n_pool > 1 else 0.0
    t_pool = (mean_pool / (std_pool / (n_pool ** 0.5))) if std_pool > 0 else 0.0
    try:
        from scipy import stats as sp_stats

        p_pool = float(2 * (1 - sp_stats.t.cdf(abs(t_pool), df=n_pool - 1)))
    except Exception:
        p_pool = None

    result = {
        "per_window": per_window,
        "pooled": {
            "n_days": n_pool,
            "live_mean": round(st.mean(pooled_live), 2),
            "live_std": round(st.stdev(pooled_live), 2),
            "live_sum": round(sum(pooled_live), 1),
            "simple_mean": round(st.mean(pooled_simple), 2),
            "simple_std": round(st.stdev(pooled_simple), 2),
            "simple_sum": round(sum(pooled_simple), 1),
            "mean_diff_simple_minus_live": round(mean_pool, 2),
            "t_stat": round(t_pool, 3),
            "p_value": round(p_pool, 4) if p_pool is not None else None,
        },
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))

    # single-day sensitivity: drop largest-|diff| day
    worst = max(all_diffs, key=lambda x: abs(x[4]))
    print(f"\nLargest single-day |diff|: {worst[0]} {worst[1]} diff={worst[4]:.1f}")
    trimmed = [d for d in pooled_diffs if d != worst[4]]
    if len(trimmed) > 1:
        m2 = st.mean(trimmed)
        s2 = st.stdev(trimmed)
        t2 = m2 / (s2 / (len(trimmed) ** 0.5)) if s2 > 0 else 0.0
        print(f"Excluding it: n={len(trimmed)} mean_diff={m2:.2f} t={t2:.3f}")

    out_path = "reports/research/channel_lab/tmf_simplify_v2_vs_v120_baseline_resim_result.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
