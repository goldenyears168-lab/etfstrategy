"""Search for a candidate among night|div_hh_weak_vol and night|expand_dn,
testing EACH SIDE ALONE (L-only / S-only) rather than full unblock (which
was already rejected for expand_dn, and looked mixed for div_hh_weak_vol).

Methodology: standard PV16 harness (continuous_gate_for_day, NQ backfill
patch, PAPER_RECIPE base), portfolio-level day-paired delta vs baseline
(unchanged specialized_cell_book()), IS (22d) then OOS (66d), t-test with
excl-top-day check. 3-holdout only if promising.
"""
from __future__ import annotations

import statistics
import sys
from copy import deepcopy

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from scipy import stats as sstats

from order.tmf_channel_config import PAPER_RECIPE
from order.tmf_channel_pv16_book import specialized_cell_book
from tmf_channel.cache_store import bar_timestamps, list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
IS_DAYS = [(d, "tx_1m_fullnight_cache_full.json") for d in JULY_DAYS] + [
    (d, "tx_1m_tick_built_fullnight_aug") for d in AUG_DAYS
]
OOS_DAYS = [
    (d, "tx_1m_fullnight_cache_full.json")
    for d in list_days(source="tx_1m_fullnight_cache_full.json")
    if d < "2026-07-08"
]
HOLDOUTS = {
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}


def _arrays(rows):
    return (
        [float(r["o"]) for r in rows],
        [float(r["h"]) for r in rows],
        [float(r["l"]) for r in rows],
        [float(r["c"]) for r in rows],
        [float(r.get("v") or 0) for r in rows],
        [f"{rows[0].get('_day','')}"],  # placeholder unused
    )


def run_day(day, source, recipe, vix):
    rows = load_day(day, source=source)
    if not rows:
        return []
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)
    recipe = deepcopy(recipe)
    recipe.setdefault("hang_anchor", "O")
    recipe["session_side_gate"] = continuous_gate_for_day(day, T, source=source)
    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    return trades or []


def net_pnl(trades):
    return sum(tr["pnl"] for tr in trades)


def paired_test(day_list, base_book_recipe, cand_recipe, vix):
    base_by_day, cand_by_day = [], []
    for day, source in day_list:
        base_t = run_day(day, source, base_book_recipe, vix)
        cand_t = run_day(day, source, cand_recipe, vix)
        base_by_day.append(net_pnl(base_t))
        cand_by_day.append(net_pnl(cand_t))
    deltas = [c - b for c, b in zip(cand_by_day, base_by_day)]
    n = len(deltas)
    mean = statistics.mean(deltas)
    sd = statistics.stdev(deltas) if n > 1 else 0.0
    t = mean / (sd / n ** 0.5) if sd > 0 else float("nan")
    p = 2 * (1 - sstats.t.cdf(abs(t), df=n - 1)) if sd > 0 else float("nan")
    idx_top = max(range(n), key=lambda i: abs(deltas[i]))
    excl = [d for i, d in enumerate(deltas) if i != idx_top]
    excl_mean = statistics.mean(excl) if excl else float("nan")
    return dict(n=n, mean=mean, t=t, p=p, excl_top_day_mean=excl_mean,
                top_day=day_list[idx_top][0], top_delta=deltas[idx_top])


def make_recipe(session, regime, block):
    recipe = deepcopy(PAPER_RECIPE)
    recipe["session_pv_book"] = deepcopy(specialized_cell_book())
    recipe["session_pv_book"][session][regime]["block"] = block
    return recipe


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    baseline = make_recipe("night", "div_hh_weak_vol", ["L", "S"])  # unchanged live book proxy

    targets = [
        ("night", "expand_dn", "L-only", ["S"]),
        ("night", "expand_dn", "S-only", ["L"]),
        ("night", "div_hh_weak_vol", "L-only", ["S"]),
        ("night", "div_hh_weak_vol", "S-only", ["L"]),
    ]

    for session, regime, label, block in targets:
        cand = make_recipe(session, regime, block)
        # baseline recipe must have BOTH other cell's block unchanged; use
        # true unchanged book for baseline every time (fresh instance)
        base = deepcopy(PAPER_RECIPE)
        base["session_pv_book"] = deepcopy(specialized_cell_book())

        print(f"\n### {session}|{regime} {label} (block={block}) ###")
        is_res = paired_test(IS_DAYS, base, cand, vix)
        print(f"  IS  n={is_res['n']} mean={is_res['mean']:.2f} t={is_res['t']:.3f} "
              f"p={is_res['p']:.3f} excl_top_day_mean={is_res['excl_top_day_mean']:.2f} "
              f"(top_day={is_res['top_day']} delta={is_res['top_delta']:.1f})")
        oos_res = paired_test(OOS_DAYS, base, cand, vix)
        print(f"  OOS n={oos_res['n']} mean={oos_res['mean']:.2f} t={oos_res['t']:.3f} "
              f"p={oos_res['p']:.3f} excl_top_day_mean={oos_res['excl_top_day_mean']:.2f} "
              f"(top_day={oos_res['top_day']} delta={oos_res['top_delta']:.1f})")

        same_dir = (is_res["mean"] > 0) == (oos_res["mean"] > 0)
        promising = same_dir and (is_res["p"] < 0.10 or oos_res["p"] < 0.10) and is_res["mean"] != 0
        print(f"  same_direction={same_dir} promising={promising}")

        if promising:
            print("  -> running 3-holdout validation")
            for hname, hsource in HOLDOUTS.items():
                hdays = [(d, hsource) for d in list_days(source=hsource)]
                hres = paired_test(hdays, base, cand, vix)
                print(f"    {hname}: n={hres['n']} mean={hres['mean']:.2f} t={hres['t']:.3f} "
                      f"p={hres['p']:.3f} excl_top_day_mean={hres['excl_top_day_mean']:.2f}")


if __name__ == "__main__":
    main()
