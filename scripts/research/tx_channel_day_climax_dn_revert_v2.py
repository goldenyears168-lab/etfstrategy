"""16-cell coverage gap audit: day|climax_dn was tuned under CELL_TUNE_V2 (hang_lo
12/hang_hi 27, early_fill_gamma 11, max_hold_bars 38) but -- unlike
day|div_hh_weak_vol / night|climax_dn / night|climax_up -- never got an individual
per-cell julsep/octdec/janmar OOS attribution check the way those three did before
being reverted. r_cost_aware_cell_tune_wf.json's own per-cell "cells" breakdown
(all_rec variant, which still includes the v2 day|climax_dn patch) shows this cell
net-negative in 2 of 3 OOS holdout windows even though the *combined book* passed
overall:
  julsep25: no trades
  octdec25: n=3,  net=-101.4, mean_day_contrib=-1.64
  janmar26: n=4,  net=-136.2, mean_day_contrib=-2.48
  w83_IS:   n=16, net=+47.3,  mean_day_contrib=+0.57  (only positive window)

This is a genuinely-unexplored per-cell gap flagged by the audit. Candidate fix:
revert day|climax_dn to its pre-v2 freeze-base parameters (hang_lo=15, hang_hi=30,
early_fill_gamma=8, max_hold_bars=30 -- i.e. delete the v2 patch for this one
cell only), leaving every other cell (incl. v3's day|normal / day|div_hh_weak_vol
blocks) untouched. TRUE re-simulation of the FULL live recipe (whole-book pooled
P&L, not cell-filtered trades) via harness.run_days, across all 4 sanctioned
windows, day-clustered t-test on the paired (alt-base) diffs.
"""
from __future__ import annotations

import json
import statistics as st
from copy import deepcopy
from math import erf, sqrt

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days
from tmf_channel.harness import run_days, summarize_days

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}


def make_revert_recipe() -> dict:
    recipe = deepcopy(PAPER_RECIPE)
    book = deepcopy(recipe["session_pv_book"])
    cell = book["day"]["climax_dn"]
    assert cell["hang_lo"] == 12.0 and cell["hang_hi"] == 27.0 and cell["max_hold_bars"] == 38, (
        f"unexpected live v2 patch values: {cell}"
    )
    # Revert to freeze-base day params (no SPECIALIZED_PATCHES entry touches
    # climax_dn either, so freeze base == pre-v2 baseline for this cell).
    cell["hang_lo"] = 15.0
    cell["hang_hi"] = 30.0
    cell["early_fill_gamma"] = 8.0
    cell["max_hold_bars"] = 30
    book["day"]["climax_dn"] = cell
    recipe["session_pv_book"] = book
    return recipe


def paired_ttest(base_nets: dict, alt_nets: dict) -> dict:
    days = sorted(set(base_nets) & set(alt_nets))
    diffs = [alt_nets[d] - base_nets[d] for d in days]
    n = len(diffs)
    if n < 2:
        return {"n_days": n, "mean_diff": None, "t": None, "p": None}
    mean = st.mean(diffs)
    sd = st.stdev(diffs)
    if sd == 0:
        return {"n_days": n, "mean_diff": mean, "t": None, "p": None}
    se = sd / (n ** 0.5)
    t = mean / se
    try:
        from scipy import stats as sstats

        p = float(2 * (1 - sstats.t.cdf(abs(t), df=n - 1)))
    except Exception:
        p = float(2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2)))))
    return {"n_days": n, "mean_diff": round(mean, 2), "sd_diff": round(sd, 2), "t": round(t, 3), "p": round(p, 5)}


def day_clustered_stats(nets: list[float]) -> dict:
    n = len(nets)
    if n < 2:
        return {"n": n, "mean": None, "std": None, "sharpe": None}
    mean = st.mean(nets)
    sd = st.stdev(nets)
    wins = sum(1 for x in nets if x > 0)
    return {
        "n": n,
        "mean": round(mean, 2),
        "std": round(sd, 2),
        "sharpe": round(mean / sd, 4) if sd else None,
        "win_rate": round(100.0 * wins / n, 1),
        "sum": round(sum(nets), 1),
    }


def main():
    base_recipe = deepcopy(PAPER_RECIPE)
    alt_recipe = make_revert_recipe()

    report = {}
    pooled_base_nets = []
    pooled_alt_nets = []
    per_window_summary = {}

    for wname, cache in WINDOWS.items():
        days = list_days(cache)
        if not days:
            report[wname] = {"error": "no days found", "cache": cache}
            continue
        base_rows = run_days(days, recipe=base_recipe, cache_name=cache)
        alt_rows = run_days(days, recipe=alt_recipe, cache_name=cache)

        base_ok = {r["day"]: r for r in base_rows if r.get("ok")}
        alt_ok = {r["day"]: r for r in alt_rows if r.get("ok")}
        base_nets = {d: r["net"] for d, r in base_ok.items()}
        alt_nets = {d: r["net"] for d, r in alt_ok.items()}

        base_sum = summarize_days(base_rows)
        alt_sum = summarize_days(alt_rows)
        tt = paired_ttest(base_nets, alt_nets)

        pooled_base_nets.extend(base_nets.values())
        pooled_alt_nets.extend(alt_nets.values())

        per_window_summary[wname] = {
            "n_days": base_sum["n_days"],
            "base_mean": base_sum["mean"],
            "alt_mean": alt_sum["mean"],
            "base_wr_days": base_sum["wr_days"],
            "alt_wr_days": alt_sum["wr_days"],
            "paired_ttest_alt_minus_base": tt,
        }
        report[wname] = {
            "cache": cache,
            "baseline": base_sum,
            "alt_revert_day_climax_dn": alt_sum,
            "paired_ttest_alt_minus_base": tt,
        }
        print(f"=== {wname} ({cache}) ===")
        print(f"  baseline: n_days={base_sum['n_days']} net={base_sum['net']} mean={base_sum['mean']} wr_days={base_sum['wr_days']}")
        print(f"  alt(revert climax_dn): n_days={alt_sum['n_days']} net={alt_sum['net']} mean={alt_sum['mean']} wr_days={alt_sum['wr_days']}")
        print(f"  paired ttest (alt-base): {tt}")
        print()

    base_pooled = day_clustered_stats(pooled_base_nets)
    alt_pooled = day_clustered_stats(pooled_alt_nets)
    pooled_tt = paired_ttest(
        {i: v for i, v in enumerate(pooled_base_nets)},
        {i: v for i, v in enumerate(pooled_alt_nets)},
    )

    # single-day-artifact check on pooled diffs
    all_days = []
    for wname, cache in WINDOWS.items():
        days = list_days(cache)
        all_days.extend([(wname, d) for d in days])
    base_rows_all = {}
    alt_rows_all = {}
    for wname, cache in WINDOWS.items():
        days = list_days(cache)
        for r in run_days(days, recipe=base_recipe, cache_name=cache):
            if r.get("ok"):
                base_rows_all[(wname, r["day"])] = r["net"]
        for r in run_days(days, recipe=alt_recipe, cache_name=cache):
            if r.get("ok"):
                alt_rows_all[(wname, r["day"])] = r["net"]
    common = sorted(set(base_rows_all) & set(alt_rows_all))
    diffs_by_day = {k: alt_rows_all[k] - base_rows_all[k] for k in common}
    if diffs_by_day:
        best_day = max(diffs_by_day, key=lambda k: diffs_by_day[k])
        worst_day = min(diffs_by_day, key=lambda k: diffs_by_day[k])
        total_diff = sum(diffs_by_day.values())
        single_day_check = {
            "best_day": {"key": list(best_day), "diff": round(diffs_by_day[best_day], 1)},
            "worst_day": {"key": list(worst_day), "diff": round(diffs_by_day[worst_day], 1)},
            "total_diff": round(total_diff, 1),
            "total_diff_excl_best": round(total_diff - diffs_by_day[best_day], 1),
            "total_diff_excl_worst": round(total_diff - diffs_by_day[worst_day], 1),
        }
    else:
        single_day_check = {}

    result = {
        "title": "day|climax_dn v2 patch revert -- true re-simulation, full-book pooled",
        "baseline_pooled": base_pooled,
        "alt_pooled": alt_pooled,
        "pooled_paired_ttest": pooled_tt,
        "per_window": per_window_summary,
        "single_day_check": single_day_check,
        "detail": report,
    }
    out_path = "reports/research/channel_lab/day_climax_dn_revert_v2_result.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print("=== POOLED (all 4 windows) ===")
    print("baseline:", base_pooled)
    print("alt (revert day|climax_dn):", alt_pooled)
    print("pooled paired ttest (alt-base):", pooled_tt)
    print("single-day check:", single_day_check)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
