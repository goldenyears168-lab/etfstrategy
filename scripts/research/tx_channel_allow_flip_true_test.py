"""allow_flip audit: TRUE re-simulation of allow_flip=True vs current live
PAPER_RECIPE (final_v1_4_0_pv16_normal_dhwv_block, allow_flip=False), across
all 4 sanctioned windows (w83/julsep25/octdec25/janmar26), day-clustered
t-test, per-window breakdown, single-day-artifact check.

Mechanism (engine, causal_engine.py L1591/L1669-1670/L1689-1690 and the
identical tick_native block at L2641/L2654/L2682): when the opposite-side
hung limit fills while a position is open, the engine currently only
opp_covers to flat. With allow_flip=True, immediately after that same-tick
cover it also opens a new lot on the new side at the same fill price
("flip" tag) if flat and max_lots not exceeded -- i.e. same-bar direct
reversal instead of flat-then-wait-for-next-hang-fill. No other recipe
field changes; PV16 book, hang/cover bands, hybrid_trail exits, struct_break
all untouched.
"""
from __future__ import annotations

import json
import statistics as st
from copy import deepcopy

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days
from tmf_channel.harness import run_days, summarize_days

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}


def day_clustered_ttest(base_nets: dict, alt_nets: dict):
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
    from math import erf, sqrt
    try:
        from scipy import stats as sstats
        p = float(2 * (1 - sstats.t.cdf(abs(t), df=n - 1)))
    except Exception:
        p = float(2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2)))))
    return {"n_days": n, "mean_diff": round(mean, 2), "sd_diff": round(sd, 2), "t": round(t, 3), "p": round(p, 4)}


def sharpe(nets: list[float]) -> float | None:
    if len(nets) < 2:
        return None
    m = st.mean(nets)
    sd = st.stdev(nets)
    return round(m / sd, 4) if sd else None


def main():
    base_recipe = deepcopy(PAPER_RECIPE)
    alt_recipe = deepcopy(PAPER_RECIPE)
    alt_recipe["allow_flip"] = True

    report = {}
    pooled_base: dict[str, float] = {}
    pooled_alt: dict[str, float] = {}

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
        pooled_base.update({f"{wname}:{d}": v for d, v in base_nets.items()})
        pooled_alt.update({f"{wname}:{d}": v for d, v in alt_nets.items()})

        base_sum = summarize_days(base_rows)
        alt_sum = summarize_days(alt_rows)
        tt = day_clustered_ttest(base_nets, alt_nets)

        common = sorted(set(base_nets) & set(alt_nets))
        diffs = {d: alt_nets[d] - base_nets[d] for d in common}
        best_day = max(diffs, key=diffs.get) if diffs else None
        worst_day = min(diffs, key=diffs.get) if diffs else None
        mean_diff_excl_best = (
            round((sum(diffs.values()) - diffs[best_day]) / (len(diffs) - 1), 2)
            if len(diffs) > 1 else None
        )
        mean_diff_excl_worst = (
            round((sum(diffs.values()) - diffs[worst_day]) / (len(diffs) - 1), 2)
            if len(diffs) > 1 else None
        )

        base_vals = list(base_nets.values())
        alt_vals = list(alt_nets.values())
        report[wname] = {
            "cache": cache,
            "n_days": len(common),
            "baseline": {"mean": base_sum["mean"], "std": round(st.stdev(base_vals), 1) if len(base_vals) > 1 else None,
                         "sharpe": sharpe(base_vals), "sum": base_sum["net"], "win_rate": base_sum.get("wr_days")},
            "allow_flip_true": {"mean": alt_sum["mean"], "std": round(st.stdev(alt_vals), 1) if len(alt_vals) > 1 else None,
                                 "sharpe": sharpe(alt_vals), "sum": alt_sum["net"], "win_rate": alt_sum.get("wr_days")},
            "day_clustered_ttest_alt_minus_base": tt,
            "best_day_diff": {"day": best_day, "diff": round(diffs[best_day], 1)} if best_day else None,
            "worst_day_diff": {"day": worst_day, "diff": round(diffs[worst_day], 1)} if worst_day else None,
            "mean_diff_excl_best_day": mean_diff_excl_best,
            "mean_diff_excl_worst_day": mean_diff_excl_worst,
        }
        print(f"=== {wname} ({cache}) n_days={len(common)} ===")
        print(f"  baseline:        mean={report[wname]['baseline']['mean']} std={report[wname]['baseline']['std']} sharpe={report[wname]['baseline']['sharpe']} sum={report[wname]['baseline']['sum']}")
        print(f"  allow_flip=True: mean={report[wname]['allow_flip_true']['mean']} std={report[wname]['allow_flip_true']['std']} sharpe={report[wname]['allow_flip_true']['sharpe']} sum={report[wname]['allow_flip_true']['sum']}")
        print(f"  ttest(alt-base): {tt}")
        print(f"  best/worst day diff: {report[wname]['best_day_diff']} / {report[wname]['worst_day_diff']}")
        print(f"  mean_diff excl best/worst: {mean_diff_excl_best} / {mean_diff_excl_worst}")
        print()

    # pooled across all 265 days
    common_all = sorted(set(pooled_base) & set(pooled_alt))
    base_all = [pooled_base[d] for d in common_all]
    alt_all = [pooled_alt[d] for d in common_all]
    tt_all = day_clustered_ttest({d: pooled_base[d] for d in common_all}, {d: pooled_alt[d] for d in common_all})
    pooled_summary = {
        "n_days": len(common_all),
        "baseline": {"mean": round(st.mean(base_all), 2), "std": round(st.stdev(base_all), 2), "sharpe": sharpe(base_all), "sum": round(sum(base_all), 1)},
        "allow_flip_true": {"mean": round(st.mean(alt_all), 2), "std": round(st.stdev(alt_all), 2), "sharpe": sharpe(alt_all), "sum": round(sum(alt_all), 1)},
        "day_clustered_ttest_alt_minus_base": tt_all,
    }
    diffs_all = {d: pooled_alt[d] - pooled_base[d] for d in common_all}
    best_all = max(diffs_all, key=diffs_all.get)
    worst_all = min(diffs_all, key=diffs_all.get)
    pooled_summary["best_day_diff"] = {"day": best_all, "diff": round(diffs_all[best_all], 1)}
    pooled_summary["worst_day_diff"] = {"day": worst_all, "diff": round(diffs_all[worst_all], 1)}
    pooled_summary["mean_diff_excl_best_day"] = round((sum(diffs_all.values()) - diffs_all[best_all]) / (len(diffs_all) - 1), 2)
    pooled_summary["mean_diff_excl_worst_day"] = round((sum(diffs_all.values()) - diffs_all[worst_all]) / (len(diffs_all) - 1), 2)
    report["pooled"] = pooled_summary
    print("=== POOLED (all 4 windows) ===")
    print(json.dumps(pooled_summary, indent=2, default=str))

    out_path = "reports/research/channel_lab/allow_flip_true_v1_4_0_result.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
