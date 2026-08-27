#!/usr/bin/env python3
"""Cell-tune (2026-08-09): day|dry under the 30-min-primary/1-min-calib PV8
architecture (scripts/research/tmf_30m_primary_1m_calib_prototype.py).

ASSIGNED CELL: day|dry only. All other 15 cells stay at the current
live-equivalent default (order.tmf_channel_pv16_book.specialized_cell_book(),
i.e. SPECIALIZED_PATCHES + CELL_TUNE_V2_PATCHES on freeze_cell_book()) for
every run in this script -- only day|dry's hang_lo/hang_hi/max_hold_bars/
early_fill_gamma/block are varied.

Architecture under test: PV8 regime selection is driven by 30-min bars
(recalibrated thresholds, see prototype module docstring) while ALL
execution mechanics (hang levels, fills, exits, struct_break, trail, stop,
max_hold) stay on 1-min granularity exactly as in the live engine. The
16-cell book's PARAMETERS (this script only varies day|dry) are looked up
per-1-min-bar exactly as today; only the *regime label* feeding that lookup
now updates every 30 min instead of ~every 1-min bar.

Methodology: day-clustered paired comparison (candidate vs baseline
current-live-equivalent book, both run under the SAME 30m-primary regime
feed) across the 22-day in-sample window, then OOS-validate the single best
in-sample candidate against the 66-day pre-07-08 window. Never mixes 1m-native
and 30m-primary numbers -- both baseline and candidate here run under the
30m-primary regime feed, so the comparison isolates day|dry's OWN params,
not the architecture change itself (that's the prototype script's job).

Does NOT touch src/order/*.py, src/tmf_channel/causal_engine.py,
config/order.yaml, .env, launchd/, scripts/order/, config/strategy.yaml,
config/strategies.yaml.
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

OOS_DAYS = [
    d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"
]
for d in OOS_DAYS:
    SOURCE_FOR_DAY[d] = "tx_1m_fullnight_cache_full.json"

MY_SESS = "day"
MY_PV = "dry"

BASE_CELL = dict(specialized_cell_book()["day"]["dry"])
# = {hang_lo: 13.0, hang_hi: 28.0, early_fill_gamma: 8.0, max_hold_bars: 30,
#    block: [], skip_quiet_mode: "dry", bias: True, vixtwn_calib: "blend",
#    vixtwn_calib_gamma: 5.0}

CANDIDATES: dict[str, dict] = {
    "current_default": {},
    # Wider band (30-min dwell is coarser/more persistent than 1-min flicker
    # -> a regime that now holds for ~30min may tolerate a wider, less
    # twitchy hang band instead of the tight 1-min-tuned 13-28).
    "wide_a": dict(hang_lo=20.0, hang_hi=40.0),
    "wide_b": dict(hang_lo=25.0, hang_hi=50.0),
    "wide_a_longhold": dict(hang_lo=20.0, hang_hi=40.0, max_hold_bars=45),
    "wide_b_longhold": dict(hang_lo=25.0, hang_hi=50.0, max_hold_bars=60),
    "narrow_shorthold": dict(hang_lo=13.0, hang_hi=28.0, max_hold_bars=15),
    "wide_a_gamma_up": dict(hang_lo=20.0, hang_hi=40.0, early_fill_gamma=14.0),
    "wide_a_gamma0": dict(hang_lo=20.0, hang_hi=40.0, early_fill_gamma=0.0),
    "hold_only_45": dict(max_hold_bars=45),
    "hold_only_60": dict(max_hold_bars=60),
    "blocked": dict(block=["L", "S"]),
}


def _book_with(cell_override: dict) -> dict:
    book = deepcopy(specialized_cell_book())
    book[MY_SESS][MY_PV] = {**BASE_CELL, **cell_override}
    return book


def run_day(day: str, book: dict, recipe_base: dict, vix: dict) -> dict:
    source = SOURCE_FOR_DAY[day]
    rows = load_day(day, source=source)
    if not rows:
        return dict(day=day, n_trades=0, my_net=0.0, my_n=0, skipped=True)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]

    pv_series = build_pv30_series(T, O, H, L, C, V)
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    try:
        recipe = dict(recipe_base)
        recipe["session_pv_book"] = book
        trades, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV

    my_trades = []
    for tr in trades:
        if str(tr.get("regime_e") or "") != MY_PV:
            continue
        hm = str(tr.get("et") or "")[11:16]
        sess = "day" if "08:45" <= hm < "13:45" else "night"
        if sess != MY_SESS:
            continue
        my_trades.append(tr)

    return dict(
        day=day,
        n_trades=len(trades),
        my_n=len(my_trades),
        my_net=round(sum(float(t["pnl"]) for t in my_trades), 1),
    )


def day_clustered_stats(deltas: list[float]) -> dict:
    n = len(deltas)
    if n == 0:
        return dict(n=0)
    mean = st.mean(deltas)
    sd = st.stdev(deltas) if n > 1 else 0.0
    t = (mean / (sd / (n ** 0.5))) if sd > 0 else (float("inf") if mean != 0 else 0.0)
    # two-sided p via normal approx (n is modest but this is descriptive,
    # not a formal inference claim)
    from math import erf, sqrt
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2)))) if sd > 0 else (0.0 if mean != 0 else 1.0)
    return dict(n=n, mean=round(mean, 2), std=round(sd, 2), t=round(t, 3), p=round(p, 4))


def evaluate(days: list[str], cell_override: dict, recipe_base: dict, vix: dict,
             baseline_cache: dict) -> dict:
    """Returns per-day my_net for candidate, plus deltas vs baseline_cache."""
    cand_book = _book_with(cell_override)
    deltas = []
    my_ns = []
    per_day = []
    for day in days:
        r = run_day(day, cand_book, recipe_base, vix)
        base = baseline_cache[day]
        delta = r["my_net"] - base["my_net"]
        deltas.append(delta)
        my_ns.append(r["my_n"])
        per_day.append(dict(day=day, cand_net=r["my_net"], base_net=base["my_net"], delta=delta,
                             cand_n=r["my_n"], base_n=base["my_n"]))
    return dict(per_day=per_day, deltas=deltas, my_ns=my_ns,
                stats=day_clustered_stats(deltas), total_appearances=sum(my_ns))


def main():
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    # 1. Baseline (current_default cell params) for every in-sample day, once.
    baseline_book = _book_with({})
    baseline_cache = {}
    for day in IN_SAMPLE_DAYS:
        baseline_cache[day] = run_day(day, baseline_book, recipe_base, vix)
        print("[baseline]", json.dumps(baseline_cache[day], ensure_ascii=False), flush=True)

    base_total_n = sum(baseline_cache[d]["my_n"] for d in IN_SAMPLE_DAYS)
    base_total_net = sum(baseline_cache[d]["my_net"] for d in IN_SAMPLE_DAYS)
    print(f"\n[baseline] total day|dry appearances={base_total_n}, net={base_total_net:.1f} "
          f"over {len(IN_SAMPLE_DAYS)} days")

    if base_total_n < 15:
        print("\n=== INSUFFICIENT DATA: fewer than ~15 day|dry appearances in-sample ===")
        return

    # 2. Sweep candidates (excluding current_default, already = baseline).
    results = {}
    for name, override in CANDIDATES.items():
        if name == "current_default":
            continue
        res = evaluate(IN_SAMPLE_DAYS, override, recipe_base, vix, baseline_cache)
        results[name] = res
        s = res["stats"]
        print(f"\n[{name}] override={override}")
        print(f"  in-sample: n_days={s.get('n')} mean_delta={s.get('mean')} std={s.get('std')} "
              f"t={s.get('t')} p={s.get('p')} total_appearances={res['total_appearances']}")

    # 3. Pick best candidate by mean day-clustered delta (only if beats 0 and
    # has enough appearances); else "current_default" wins.
    best_name = "current_default"
    best_mean = 0.0
    for name, res in results.items():
        if res["total_appearances"] < 15:
            continue
        m = res["stats"].get("mean", 0.0)
        if m > best_mean:
            best_mean = m
            best_name = name

    print(f"\n=== BEST IN-SAMPLE CANDIDATE: {best_name} (mean_delta={best_mean:.2f}) ===")

    # Red-flag check: does excluding the single largest-|delta| day flip sign?
    if best_name != "current_default":
        deltas = results[best_name]["deltas"]
        idx_max = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
        excl = deltas[:idx_max] + deltas[idx_max + 1:]
        excl_mean = st.mean(excl) if excl else 0.0
        print(f"  excluding largest-|delta| day ({IN_SAMPLE_DAYS[idx_max]}, "
              f"delta={deltas[idx_max]:.1f}): mean_delta={excl_mean:.2f} "
              f"(sign {'FLIPS' if (excl_mean > 0) != (best_mean > 0) else 'holds'})")

    # 4. OOS validation of the single best candidate only.
    oos_result = None
    if best_name != "current_default":
        baseline_book_oos = baseline_book  # same book object, session_pv_book identical
        baseline_cache_oos = {}
        for day in OOS_DAYS:
            baseline_cache_oos[day] = run_day(day, baseline_book_oos, recipe_base, vix)
        oos_res = evaluate(OOS_DAYS, CANDIDATES[best_name], recipe_base, vix, baseline_cache_oos)
        oos_result = oos_res
        s = oos_res["stats"]
        print(f"\n[OOS validate {best_name}] n_days={s.get('n')} mean_delta={s.get('mean')} "
              f"std={s.get('std')} t={s.get('t')} p={s.get('p')} "
              f"total_appearances={oos_res['total_appearances']}")

    out = dict(
        cell="day|dry",
        base_cell=BASE_CELL,
        in_sample_baseline=dict(n=base_total_n, net=base_total_net),
        in_sample_candidates={k: v["stats"] | dict(total_appearances=v["total_appearances"])
                               for k, v in results.items()},
        best_name=best_name,
        best_override=CANDIDATES.get(best_name, {}),
        oos=oos_result["stats"] if oos_result else None,
        oos_total_appearances=oos_result["total_appearances"] if oos_result else None,
    )
    # Note: reports/research/channel_lab/ is read-only for this task -- print
    # the summary dict instead of writing an artifact there.
    print("\n=== summary ===")
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
