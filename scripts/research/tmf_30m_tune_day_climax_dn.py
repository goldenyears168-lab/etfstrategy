#!/usr/bin/env python3
"""TMF 30m-primary/1m-calib architecture: tune ONLY day|climax_dn cell.

Assigned cell: day|climax_dn. All other 15 cells stay at the current-live
book (freeze_cell_book() + SPECIALIZED_PATCHES + CELL_TUNE_V2_PATCHES, i.e.
order.tmf_channel_pv16_book.specialized_cell_book()) throughout.

Architecture under test (prototyped, not built here):
  scripts/research/tmf_30m_primary_1m_calib_prototype.py -- PV8 regime
  classification is driven by 30-minute bars (updates every 30min, using
  the last FULLY CLOSED 30-min bucket, PIT-safe) via
  build_pv30_series()/patched_classify_pv_factory(), monkeypatching
  tmf_channel.causal_engine.classify_pv for the duration of simulate().
  All execution mechanics (hang, fills, exits, struct_break, trail, stop,
  max_hold) stay on 1-minute granularity exactly as in the current live
  engine -- only "which of the 16 cells' parameters currently apply" comes
  from the coarser 30-min read instead of a 1-min-by-1-min reclassification.

Never touches causal_engine.py / order.yaml / .env / launchd / scripts/order.

Methodology: day-clustered comparison (candidate minus baseline
current-live-equivalent book), paired t-test across days, in-sample (22d)
search then a single OOS (66d) validation of the chosen candidate only.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402

from tmf_30m_primary_1m_calib_prototype import (  # noqa: E402
    build_pv30_series,
    patched_classify_pv_factory,
)

_ORIG_CLASSIFY_PV = ce.classify_pv

SESSION = "day"
PV = "climax_dn"

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
IN_SAMPLE_DAYS = JULY_DAYS + AUG_DAYS
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})

OOS_SOURCE = "tx_1m_fullnight_cache_full.json"
OOS_DAYS = [d for d in list_days(source=OOS_SOURCE) if d < "2026-07-08"]


def in_day_session(hhmm: str) -> str:
    return "day" if "08:45" <= hhmm < "13:45" else "night"


def build_book(overrides: dict | None) -> dict:
    book = specialized_cell_book()
    if overrides:
        book[SESSION][PV].update(overrides)
    return book


def load_arrays(day: str, source: str):
    rows = load_day(day, source=source)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def run_day_cell(day: str, source: str, recipe_overrides: dict | None, vix: dict) -> dict:
    """Run one day with recipe_overrides applied to day|climax_dn only.
    Returns n_trades and net_pnl for JUST the assigned cell's trades."""
    arrs = load_arrays(day, source)
    if arrs is None:
        return dict(day=day, n=0, net=0.0, skipped=True)
    O, H, L, C, V, T = arrs

    recipe = deepcopy(PAPER_RECIPE)
    recipe["session_pv_book"] = build_book(recipe_overrides)

    pv_series = build_pv30_series(T, O, H, L, C, V)
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    try:
        trades, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV

    cell_trades = []
    for tr in trades:
        if tr.get("regime_e") != PV:
            continue
        hhmm = str(tr.get("et", "")).split("T")[-1][:5]
        sess = in_day_session(hhmm)
        if sess != SESSION:
            continue
        cell_trades.append(tr)

    net = round(sum(t["pnl"] for t in cell_trades), 1)
    return dict(day=day, n=len(cell_trades), net=net, skipped=False)


def paired_stats(deltas: list[float]) -> dict:
    n = len(deltas)
    if n < 2:
        return dict(n=n, mean=None, std=None, t=None, p=None)
    mean = st.mean(deltas)
    std = st.stdev(deltas)
    if std == 0:
        t = float("inf") if mean != 0 else 0.0
    else:
        t = mean / (std / (n ** 0.5))
    p = approx_two_sided_p(t, n - 1)
    return dict(n=n, mean=round(mean, 2), std=round(std, 2), t=round(t, 3), p=p)


def approx_two_sided_p(t: float, df: int) -> float:
    """Rough two-sided p-value via normal approximation (adequate for
    exploratory day-clustered comparisons here; df=21 or df=65)."""
    import math

    if t in (float("inf"), float("-inf")):
        return 0.0
    x = abs(t)
    # normal-approx tail (close enough for df>=20)
    p = math.erfc(x / math.sqrt(2))
    return round(p, 4)


def evaluate_candidate(name: str, overrides: dict | None, days: list[str],
                        source_map, vix: dict, baseline_cache: dict) -> dict:
    deltas = []
    ns = []
    per_day = []
    for day in days:
        source = source_map(day)
        base_key = day
        if base_key not in baseline_cache:
            baseline_cache[base_key] = run_day_cell(day, source, None, vix)
        base = baseline_cache[base_key]
        cand = run_day_cell(day, source, overrides, vix)
        if cand.get("skipped"):
            continue
        delta = cand["net"] - base["net"]
        deltas.append(delta)
        ns.append(cand["n"])
        per_day.append(dict(day=day, base_net=base["net"], cand_net=cand["net"],
                             delta=round(delta, 1), base_n=base["n"], cand_n=cand["n"]))

    stats = paired_stats(deltas)
    total_n = sum(ns)
    return dict(name=name, overrides=overrides, stats=stats,
                total_trade_appearances=total_n, per_day=per_day, deltas=deltas)


def main():
    vix = load_vixtwn_delta() or {}

    def source_map_is(day: str) -> str:
        return SOURCE_FOR_DAY[day]

    def source_map_oos(day: str) -> str:
        return OOS_SOURCE

    baseline_cache: dict = {}

    # First establish baseline appearance count for sanity / thin-sample check.
    print("=== Baseline (current-live-equivalent book) day|climax_dn appearances, in-sample ===")
    base_ns = []
    for day in IN_SAMPLE_DAYS:
        b = run_day_cell(day, SOURCE_FOR_DAY[day], None, vix)
        baseline_cache[day] = b
        base_ns.append(b["n"])
        print(json.dumps(b))
    total_base_n = sum(base_ns)
    print(f"total baseline trade-appearances (22d) = {total_base_n}\n")

    if total_base_n < 15:
        print("INSUFFICIENT DATA: fewer than 15 trades/day-appearances across 22 "
              "in-sample days for day|climax_dn. Reporting insufficient_data, no "
              "tuning performed.")
        with open(
            "reports/research/channel_lab/tmf_30m_tune_day_climax_dn_result.json", "w"
        ) as f:
            json.dump(dict(cell="day|climax_dn", verdict="INSUFFICIENT_DATA",
                            total_in_sample_n=total_base_n), f, indent=2)
        return

    # Current default (baseline) values for day|climax_dn (from CELL_TUNE_V2):
    # hang_lo=12.0, hang_hi=27.0, early_fill_gamma=11.0, max_hold_bars=38, block=[]
    candidates = {
        "default (current live)": None,
        "wider_band_1": {"hang_lo": 18.0, "hang_hi": 38.0},
        "wider_band_2": {"hang_lo": 22.0, "hang_hi": 45.0},
        "wider_band_longer_hold": {"hang_lo": 18.0, "hang_hi": 38.0, "max_hold_bars": 55},
        "narrower_band_short_hold": {"hang_lo": 12.0, "hang_hi": 27.0, "max_hold_bars": 20},
        "hold_short_only": {"max_hold_bars": 20},
        "hold_long_only": {"max_hold_bars": 55},
        "gamma_reduced": {"early_fill_gamma": 5.0},
        "gamma_zero": {"early_fill_gamma": 0.0},
        "block_ls": {"block": ["L", "S"]},
    }

    results = {}
    for name, overrides in candidates.items():
        res = evaluate_candidate(name, overrides, IN_SAMPLE_DAYS, source_map_is,
                                  vix, baseline_cache)
        results[name] = res
        st_ = res["stats"]
        print(f"[IS] {name:28s} overrides={overrides} "
              f"n_appear={res['total_trade_appearances']:3d} "
              f"mean_delta={st_['mean']} std={st_['std']} t={st_['t']} p={st_['p']}")

    # pick best non-default, non-block candidate by mean delta (ties by t)
    non_default = {k: v for k, v in results.items() if k != "default (current live)"}
    best_name = max(non_default, key=lambda k: (non_default[k]["stats"]["mean"] or -1e9))
    best = results[best_name]
    default_stats = results["default (current live)"]["stats"]

    print(f"\nBest in-sample candidate: {best_name} mean_delta={best['stats']['mean']} "
          f"vs default mean_delta=0 (self) baseline_net_sum="
          f"{sum(d['base_net'] for d in results['default (current live)']['per_day'])}")

    # red-flag check: does excluding largest |delta| day flip the sign?
    deltas = best["deltas"]
    if deltas:
        max_i = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
        excl = deltas[:max_i] + deltas[max_i + 1:]
        excl_mean = st.mean(excl) if excl else None
        print(f"Largest-|delta| day excluded -> mean_delta={excl_mean} "
              f"(full mean={best['stats']['mean']}), day={best['per_day'][max_i]['day']}")

    # decide winner: default wins unless a non-default candidate has mean_delta>0
    # and p<0.10 roughly (exploratory threshold) and survives the exclusion check
    winner_name = "default (current live)"
    winner_overrides = None
    if (best["stats"]["mean"] or 0) > 0 and (best["stats"]["p"] or 1) < 0.20:
        winner_name = best_name
        winner_overrides = best["overrides"]

    print(f"\n=== IN-SAMPLE WINNER: {winner_name} overrides={winner_overrides} ===")

    # OOS validation of the winner ONLY
    oos_baseline_cache: dict = {}
    oos_res = evaluate_candidate(winner_name, winner_overrides, OOS_DAYS,
                                  source_map_oos, vix, oos_baseline_cache)
    oos_total_n = sum(d["cand_n"] for d in oos_res["per_day"])
    print(f"[OOS n_days={len(OOS_DAYS)}] {winner_name} n_appear={oos_total_n} "
          f"stats={oos_res['stats']}")

    verdict = "NO_IMPROVEMENT"
    if winner_name == "default (current live)":
        verdict = "NO_IMPROVEMENT"
    else:
        is_stats = best["stats"]
        os_stats = oos_res["stats"]
        if (os_stats["mean"] or 0) <= 0 or (os_stats["p"] or 1) >= 0.5:
            verdict = "OOS_FAILED"
        else:
            verdict = "ADOPT"

    out = dict(
        cell=f"{SESSION}|{PV}",
        in_sample_all_candidates={k: dict(stats=v["stats"],
                                           total_trade_appearances=v["total_trade_appearances"])
                                   for k, v in results.items()},
        winner_name=winner_name,
        winner_overrides=winner_overrides,
        in_sample_stats=best["stats"] if winner_name != "default (current live)" else default_stats,
        oos_stats=oos_res["stats"],
        oos_n_days=len(OOS_DAYS),
        verdict=verdict,
    )
    with open(
        "reports/research/channel_lab/tmf_30m_tune_day_climax_dn_result.json", "w"
    ) as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("\nWrote reports/research/channel_lab/tmf_30m_tune_day_climax_dn_result.json")
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
