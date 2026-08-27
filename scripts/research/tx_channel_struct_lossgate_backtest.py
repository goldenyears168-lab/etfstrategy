"""struct_break loss-gate: only allow struct_break exits while the open
position is currently underwater (fav_now < 0), vs current live v1.4.0
PAPER_RECIPE baseline. True re-simulation via forked engine
(tx_channel_struct_lossgate_engine.simulate, p["struct_loss_gated"]),
day-clustered t-test, all 4 sanctioned windows, plus single-best/worst-day
exclusion check (methodology rule 3).

Rationale: tonight's struct_disabled_baseline showed fully removing
struct_break nets flat-to-slightly-negative across all windows -- it trades
away real loss protection along with the (mechanically opposed) false cuts
of flat/winning trades. This fork keeps struct_break's loss-cutting role
intact but removes its ability to cut trades that are currently at or above
breakeven, on the theory (stated in the audit brief) that struct_break is
"mechanically opposed to the weak real reversion tendency" specifically
when a trade has room to breathe -- i.e. only its in-the-money/breakeven
false-positives are costly, not its in-the-red catches.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import RECIPE_VERSION  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, summarize  # noqa: E402
from tx_channel_struct_lossgate_engine import simulate as simulate_lossgate  # noqa: E402

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
}


def _arrays_from_rows(rows, day):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def run_day_lossgate(day, *, recipe, cache_name, vix_delta):
    recipe = deepcopy(recipe)
    recipe.setdefault("hang_anchor", "O")
    recipe.setdefault("recipe_version", RECIPE_VERSION)
    rows = load_day(day, source=cache_name)
    if not rows:
        return {"ok": False, "day": day, "reason": "missing_day"}
    O, H, L, C, V, T = _arrays_from_rows(rows, day)
    trades, events, ws, wl, rvol, regime, open_pos = simulate_lossgate(
        O, H, L, C, V, T, recipe, vix_delta=vix_delta
    )
    s = summarize(trades) if trades else {"n": 0, "net": 0.0}
    return {
        "ok": True,
        "day": day,
        "n_trades": int(s.get("n") or 0),
        "net": float(s.get("net") or 0.0),
    }


def day_clustered_ttest(base_nets: dict, alt_nets: dict, exclude_day: str | None = None):
    days = sorted(set(base_nets) & set(alt_nets))
    if exclude_day:
        days = [d for d in days if d != exclude_day]
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
        from math import erf, sqrt
        p = float(2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2)))))
    return {"n_days": n, "mean_diff": round(mean, 1), "sd_diff": round(sd, 1), "t": round(t, 3), "p": round(p, 4)}


def main():
    base_recipe = deepcopy(PAPER_RECIPE)
    alt_recipe = deepcopy(PAPER_RECIPE)
    alt_recipe["struct_loss_gated"] = True

    vix = load_vixtwn_delta() or {}

    report = {}
    all_base_nets = {}
    all_alt_nets = {}
    for wname, cache in WINDOWS.items():
        days = list_days(cache)
        if not days:
            report[wname] = {"error": "no days found for source", "cache": cache}
            continue

        base_rows = [run_day_lossgate(d, recipe=base_recipe, cache_name=cache, vix_delta=vix) for d in days]
        alt_rows = [run_day_lossgate(d, recipe=alt_recipe, cache_name=cache, vix_delta=vix) for d in days]

        base_ok = {r["day"]: r for r in base_rows if r.get("ok")}
        alt_ok = {r["day"]: r for r in alt_rows if r.get("ok")}

        base_nets = {d: r["net"] for d, r in base_ok.items()}
        alt_nets = {d: r["net"] for d, r in alt_ok.items()}
        all_base_nets.update({f"{wname}|{d}": v for d, v in base_nets.items()})
        all_alt_nets.update({f"{wname}|{d}": v for d, v in alt_nets.items()})

        n_days = len(base_nets)
        base_mean = sum(base_nets.values()) / n_days if n_days else 0.0
        alt_mean = sum(alt_nets.values()) / n_days if n_days else 0.0
        base_trades = sum(r["n_trades"] for r in base_ok.values())
        alt_trades = sum(r["n_trades"] for r in alt_ok.values())

        tt = day_clustered_ttest(base_nets, alt_nets)

        # single-day exclusion check: drop the day with the largest |diff|
        common_days = sorted(set(base_nets) & set(alt_nets))
        diffs_by_day = {d: alt_nets[d] - base_nets[d] for d in common_days}
        worst_day = max(diffs_by_day, key=lambda d: abs(diffs_by_day[d])) if diffs_by_day else None
        tt_excl = day_clustered_ttest(base_nets, alt_nets, exclude_day=worst_day) if worst_day else None

        report[wname] = {
            "cache": cache,
            "n_days": n_days,
            "base_net": round(sum(base_nets.values()), 1),
            "base_mean": round(base_mean, 1),
            "base_trades_total": base_trades,
            "alt_net": round(sum(alt_nets.values()), 1),
            "alt_mean": round(alt_mean, 1),
            "alt_trades_total": alt_trades,
            "day_clustered_ttest_alt_minus_base": tt,
            "largest_single_day_diff_day": worst_day,
            "largest_single_day_diff_pt": round(diffs_by_day.get(worst_day, 0.0), 1) if worst_day else None,
            "ttest_excluding_that_day": tt_excl,
        }
        print(f"=== {wname} ({cache}) n_days={n_days} ===")
        print(f"  base: net={report[wname]['base_net']} mean={report[wname]['base_mean']} trades={base_trades}")
        print(f"  lossgate: net={report[wname]['alt_net']} mean={report[wname]['alt_mean']} trades={alt_trades}")
        print(f"  ttest(alt-base): {tt}")
        print(f"  worst single-day diff: {worst_day} = {report[wname]['largest_single_day_diff_pt']}pt; excl-ttest: {tt_excl}")
        print()

    # pooled (unweighted per-day, across all windows)
    common = sorted(set(all_base_nets) & set(all_alt_nets))
    pooled_base = [all_base_nets[k] for k in common]
    pooled_alt = [all_alt_nets[k] for k in common]
    n = len(common)
    base_mean, base_sd = st.mean(pooled_base), st.stdev(pooled_base)
    alt_mean, alt_sd = st.mean(pooled_alt), st.stdev(pooled_alt)
    pooled_tt = day_clustered_ttest(dict(zip(common, pooled_base)), dict(zip(common, pooled_alt)))
    print("=== POOLED (all 4 windows, n_days=%d) ===" % n)
    print(f"  base:      mean={base_mean:.1f} std={base_sd:.1f} sharpe={base_mean/base_sd:.3f}")
    print(f"  lossgate:  mean={alt_mean:.1f} std={alt_sd:.1f} sharpe={alt_mean/alt_sd:.3f}")
    print(f"  ttest(alt-base): {pooled_tt}")

    report["pooled"] = {
        "n_days": n,
        "base_mean": round(base_mean, 1), "base_sd": round(base_sd, 1), "base_sharpe": round(base_mean / base_sd, 4),
        "alt_mean": round(alt_mean, 1), "alt_sd": round(alt_sd, 1), "alt_sharpe": round(alt_mean / alt_sd, 4),
        "ttest_alt_minus_base": pooled_tt,
    }

    out_path = "reports/research/channel_lab/struct_lossgate_v1_4_0_result.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
