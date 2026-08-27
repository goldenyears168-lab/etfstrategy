#!/usr/bin/env python3
"""Item #16: re-verify vix_session_bias ablation on CURRENT PAPER_RECIPE
(v1.4.0, RECIPE_VERSION=final_v1_4_0_pv16_normal_dhwv_block).

Prior ablations (vix_session_bias_fullnight_oos_check.py,
vix_lag1_session_bias_ablation.py, vix_crash_rally_short_bias_lab.py, all
under reports/research/channel_lab/) ran on the OLD jack_channel_v5/v6_pv
engine with a hand-copied "V11" recipe dict (Final v1.1.1), predating the
day-normal / day-div_hh_weak_vol block and predating causal_engine.py
entirely. This script re-runs the ON vs OFF ablation via true re-simulation
on tmf_channel.causal_engine.simulate() / harness.run_days(), across w83
(2026-04-01..07-31) plus all 3 holdouts (janmar, julsep, octdec) —
270 total sessions — with day-clustered paired t-tests.

Read-only research. No live params touched.
"""
from __future__ import annotations

import copy
import json
import statistics
from pathlib import Path

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.harness import run_days, summarize_days, recipe_hash

OUT = Path(__file__).resolve().parents[2] / "reports/research/channel_lab/vix_session_bias_v14_recheck.json"

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "janmar_holdout": "tx_1m_janmar_holdout_cache.json",
    "julsep_holdout": "tx_1m_julsep_holdout_cache.json",
    "octdec_holdout": "tx_1m_octdec_holdout_cache.json",
}


def paired_ttest(diffs: list[float]) -> tuple[float | None, float | None]:
    """Day-clustered one-sample t-test on per-day (on-off) net diffs."""
    n = len(diffs)
    if n < 2:
        return None, None
    mean = statistics.mean(diffs)
    sd = statistics.stdev(diffs)
    if sd == 0:
        return (float("inf") if mean != 0 else 0.0), (0.0 if mean != 0 else 1.0)
    t = mean / (sd / (n ** 0.5))
    # two-sided p via normal approx (n large enough across pooled windows;
    # per-window n as low as 55 — report t, let reader judge with df=n-1)
    from math import erf, sqrt
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))
    return t, p


def run_window(cache_name: str, days: list[str]) -> dict:
    on_recipe = copy.deepcopy(PAPER_RECIPE)
    on_recipe["vix_session_bias"] = True
    off_recipe = copy.deepcopy(PAPER_RECIPE)
    off_recipe["vix_session_bias"] = False

    on_rows = run_days(days, recipe=on_recipe, cache_name=cache_name)
    off_rows = run_days(days, recipe=off_recipe, cache_name=cache_name)

    on_ok = {r["day"]: r for r in on_rows if r.get("ok")}
    off_ok = {r["day"]: r for r in off_rows if r.get("ok")}
    common = sorted(set(on_ok) & set(off_ok))

    diffs = [on_ok[d]["net"] - off_ok[d]["net"] for d in common]
    t, p = paired_ttest(diffs)

    on_sum = summarize_days(on_rows)
    off_sum = summarize_days(off_rows)

    n_diff_days = sum(1 for d in diffs if abs(d) > 1e-9)

    return dict(
        cache=cache_name,
        n_days=len(common),
        n_days_with_any_diff=n_diff_days,
        on=dict(net=on_sum["net"], mean=on_sum["mean"], wr_days=on_sum["wr_days"],
                 trades_total=on_sum["trades_total"]),
        off=dict(net=off_sum["net"], mean=off_sum["mean"], wr_days=off_sum["wr_days"],
                  trades_total=off_sum["trades_total"]),
        net_diff_on_minus_off=round(on_sum["net"] - off_sum["net"], 1),
        mean_daily_diff=round(statistics.mean(diffs), 2) if diffs else None,
        stdev_daily_diff=round(statistics.stdev(diffs), 2) if len(diffs) > 1 else None,
        t_stat=round(t, 3) if t is not None else None,
        p_value=round(p, 4) if p is not None else None,
        on_recipe_hash=recipe_hash(on_recipe),
        off_recipe_hash=recipe_hash(off_recipe),
    )


def main():
    from tmf_channel.cache_store import list_days

    out = {"item": "#16 vix_session_bias re-verify on v1.4.0", "windows": {}}
    all_diffs = []
    for label, cache in WINDOWS.items():
        days = list_days(source=cache)
        res = run_window(cache, days)
        out["windows"][label] = res
        print(f"{label:16s} n={res['n_days']:3d} diff_days={res['n_days_with_any_diff']:3d} "
              f"net_diff={res['net_diff_on_minus_off']:9.1f} "
              f"mean/day={res['mean_daily_diff']} t={res['t_stat']} p={res['p_value']}")

    # pooled across all 4 windows (each window's per-day diffs, treating
    # windows as independent day-clustered samples; also report a naive
    # pooled t-test across all 270 days as a secondary check)
    pooled_days = []
    for label, cache in WINDOWS.items():
        days = list_days(source=cache)
        on_recipe = copy.deepcopy(PAPER_RECIPE)
        on_recipe["vix_session_bias"] = True
        off_recipe = copy.deepcopy(PAPER_RECIPE)
        off_recipe["vix_session_bias"] = False
        on_rows = {r["day"]: r for r in run_days(days, recipe=on_recipe, cache_name=cache) if r.get("ok")}
        off_rows = {r["day"]: r for r in run_days(days, recipe=off_recipe, cache_name=cache) if r.get("ok")}
        common = sorted(set(on_rows) & set(off_rows))
        pooled_days.extend(on_rows[d]["net"] - off_rows[d]["net"] for d in common)

    t_all, p_all = paired_ttest(pooled_days)
    out["pooled_all_270_days"] = dict(
        n_days=len(pooled_days),
        net_diff_sum=round(sum(pooled_days), 1),
        mean_daily_diff=round(statistics.mean(pooled_days), 2) if pooled_days else None,
        t_stat=round(t_all, 3) if t_all is not None else None,
        p_value=round(p_all, 4) if p_all is not None else None,
    )
    print(f"\npooled n={len(pooled_days)} net_diff_sum={out['pooled_all_270_days']['net_diff_sum']} "
          f"mean/day={out['pooled_all_270_days']['mean_daily_diff']} "
          f"t={out['pooled_all_270_days']['t_stat']} p={out['pooled_all_270_days']['p_value']}")

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nsaved -> {OUT}")


if __name__ == "__main__":
    main()
