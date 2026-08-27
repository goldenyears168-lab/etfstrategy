"""Item #8: struct_break fully disabled baseline, true re-simulation vs current
PAPER_RECIPE (v1.4.0), across w83 + 3 holdout windows, day-clustered t-test.

recipe['struct_disabled'] = True -> engine skips struct_break exits entirely,
relying only on stop_pts + trail + opp_cover (see causal_engine.py L1626/L1983).
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
    "janmar_holdout": "tx_1m_janmar_holdout_cache.json",
    "julsep_holdout": "tx_1m_julsep_holdout_cache.json",
    "octdec_holdout": "tx_1m_octdec_holdout_cache.json",
}


def day_clustered_ttest(base_nets: dict, alt_nets: dict):
    """Paired one-sample t-test on (alt-base) diffs over days common to both."""
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
    # two-sided p via normal approx (n moderate/large in all windows here);
    # for small n we still report t, p is approximate.
    from math import erf, sqrt

    # Welch-Satterthwaite not needed (single sample); use t-distribution CDF approx
    try:
        from scipy import stats as sstats

        p = float(2 * (1 - sstats.t.cdf(abs(t), df=n - 1)))
    except Exception:
        # normal approx fallback
        p = float(2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2)))))
    return {"n_days": n, "mean_diff": round(mean, 1), "sd_diff": round(sd, 1), "t": round(t, 3), "p": round(p, 4)}


def main():
    base_recipe = deepcopy(PAPER_RECIPE)
    alt_recipe = deepcopy(PAPER_RECIPE)
    alt_recipe["struct_disabled"] = True

    report = {}
    for wname, cache in WINDOWS.items():
        days = list_days(cache)
        if not days:
            report[wname] = {"error": "no days found for source", "cache": cache}
            continue
        base_rows = run_days(days, recipe=base_recipe, cache_name=cache)
        alt_rows = run_days(days, recipe=alt_recipe, cache_name=cache)

        base_ok = {r["day"]: r for r in base_rows if r.get("ok")}
        alt_ok = {r["day"]: r for r in alt_rows if r.get("ok")}

        base_nets = {d: r["net"] for d, r in base_ok.items()}
        alt_nets = {d: r["net"] for d, r in alt_ok.items()}

        base_sum = summarize_days(base_rows)
        alt_sum = summarize_days(alt_rows)
        tt = day_clustered_ttest(base_nets, alt_nets)

        # also count struct_break trade removal impact roughly via trade count diff
        report[wname] = {
            "cache": cache,
            "n_days_total": len(days),
            "baseline": base_sum,
            "struct_disabled": alt_sum,
            "day_clustered_ttest_alt_minus_base": tt,
        }
        print(f"=== {wname} ({cache}) ===")
        print(f"  baseline:        n_days={base_sum['n_days']} net={base_sum['net']} mean={base_sum['mean']} trades_total={base_sum['trades_total']}")
        print(f"  struct_disabled: n_days={alt_sum['n_days']} net={alt_sum['net']} mean={alt_sum['mean']} trades_total={alt_sum['trades_total']}")
        print(f"  day-clustered ttest (alt-base): {tt}")
        print()

    out_path = "reports/research/channel_lab/struct_disabled_baseline_v1_4_0_result.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
