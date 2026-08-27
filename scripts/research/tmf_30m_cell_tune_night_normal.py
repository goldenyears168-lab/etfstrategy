#!/usr/bin/env python3
"""Cell-tune: night|normal under the 30m-primary/1m-calib architecture.

Assigned cell: session=night, pv=normal.

Architecture (see scripts/research/tmf_30m_primary_1m_calib_prototype.py,
which this script reuses verbatim -- NOT reimplemented): PV8 regime
classification is driven by 30-minute bars (recalibrated thresholds),
updating only every 30 min; all execution mechanics (hang, fill, exit,
struct_break, trail, stop, max_hold) stay on 1-minute granularity via the
unmodified src/tmf_channel/causal_engine.py.

Baseline: order.tmf_channel_pv16_book.specialized_cell_book() -- the
CURRENT-live-equivalent 16-cell book. Under that book, night|normal is
FULLY BLOCKED (block=["L","S"], set by CELL_TUNE_V2_PATCHES) -- i.e. the
live default for this cell already takes zero trades. This script asks: now
that PV8 classification is 30-min-driven (coarser, more stable read) rather
than 1-min-driven (flickery), does UNBLOCKING night|normal with new
hang/hold parameters recover a positive, day-clustered-significant edge?

Only night|normal varies across candidates; all other 15 cells stay at the
specialized_cell_book() default throughout (identical to baseline).

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

sys.path.insert(0, "scripts/research")
from tmf_30m_primary_1m_calib_prototype import build_pv30_series  # noqa: E402

_ORIG_CLASSIFY_PV = ce.classify_pv

SESS = "night"
PV = "normal"

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

_ALL_OOS = list_days(source="tx_1m_fullnight_cache_full.json")
OOS_DAYS = [d for d in _ALL_OOS if d < "2026-07-08"]
for d in OOS_DAYS:
    SOURCE_FOR_DAY[d] = "tx_1m_fullnight_cache_full.json"

# The current-live-equivalent pre-block cell params for night|normal (base +
# SPECIALIZED_PATCHES early_fill_gamma=9.0), for reference / as a candidate
# starting point once unblocked.
_BASE_CELL = specialized_cell_book()[SESS][PV]

CANDIDATES: dict[str, dict] = {
    "current_default_blocked": dict(_BASE_CELL),  # block=["L","S"] already
    "unblock_asis": {**_BASE_CELL, "block": []},
    "unblock_wide": {**_BASE_CELL, "block": [], "hang_lo": 25.0, "hang_hi": 45.0},
    "unblock_verywide": {
        **_BASE_CELL, "block": [], "hang_lo": 30.0, "hang_hi": 55.0,
        "max_hold_bars": 30,
    },
    "unblock_longhold": {**_BASE_CELL, "block": [], "max_hold_bars": 40},
    "unblock_wide_longhold": {
        **_BASE_CELL, "block": [], "hang_lo": 25.0, "hang_hi": 45.0,
        "max_hold_bars": 40,
    },
}


def _session_of(hm: str) -> str:
    return "night" if (hm >= "15:00" or hm < "05:00") else "day"


def build_book(cell_params: dict) -> dict:
    book = deepcopy(specialized_cell_book())
    book[SESS][PV] = dict(cell_params)
    return book


def run_day(day: str, book: dict, vix: dict) -> dict:
    source = SOURCE_FOR_DAY[day]
    rows = load_day(day, source=source)
    if not rows:
        return dict(day=day, n=0, net=0.0, skipped=True)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]

    pv_series = build_pv30_series(T, O, H, L, C, V)

    def _patched(C_, O_, rvol_, t, look=5):
        return pv_series[t], 0.0

    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    recipe["session_pv_book"] = book

    ce.classify_pv = _patched
    try:
        trades, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV

    # trades carry entry bar index `eb` (into T), not a raw HH:MM -- resolve
    # entry session via T[eb].
    cell_trades = []
    for tr in trades:
        if tr.get("regime_e") != PV:
            continue
        eb = tr.get("eb")
        if eb is None or not (0 <= int(eb) < len(T)):
            continue
        hm = T[int(eb)][11:16]
        if _session_of(hm) == SESS:
            cell_trades.append(tr)

    net = round(sum(float(tr["pnl"]) for tr in cell_trades), 1)
    return dict(day=day, n=len(cell_trades), net=net, skipped=False)


def paired_stats(deltas: list[float]) -> dict:
    n = len(deltas)
    if n < 2:
        return dict(n=n, mean=None, std=None, t=None, p=None)
    mean = st.mean(deltas)
    sd = st.stdev(deltas)
    if sd == 0:
        return dict(n=n, mean=round(mean, 2), std=0.0, t=None, p=None)
    t_stat = mean / (sd / (n ** 0.5))
    # two-sided p-value via normal approx (no scipy dependency assumed) --
    # use a light t-distribution survival approximation.
    try:
        from scipy import stats as sstats  # type: ignore

        p = float(2 * sstats.t.sf(abs(t_stat), df=n - 1))
    except Exception:
        # crude normal-approx fallback
        import math

        p = float(2 * (1 - 0.5 * (1 + math.erf(abs(t_stat) / math.sqrt(2)))))
    return dict(n=n, mean=round(mean, 2), std=round(sd, 2), t=round(t_stat, 3), p=round(p, 4))


def run_set(days: list[str], book_candidate: dict, book_baseline: dict, vix: dict) -> dict:
    per_day = []
    for day in days:
        rc = run_day(day, book_candidate, vix)
        rb = run_day(day, book_baseline, vix)
        if rc.get("skipped") or rb.get("skipped"):
            continue
        delta = rc["net"] - rb["net"]
        per_day.append(dict(day=day, cand_n=rc["n"], cand_net=rc["net"],
                             base_n=rb["n"], base_net=rb["net"], delta=delta))
    deltas = [r["delta"] for r in per_day]
    stats = paired_stats(deltas)
    total_cand_trades = sum(r["cand_n"] for r in per_day)
    return dict(per_day=per_day, stats=stats, total_cand_trades=total_cand_trades)


def main():
    vix = load_vixtwn_delta() or {}
    baseline_book = build_book(CANDIDATES["current_default_blocked"])

    print(f"=== night|normal cell tune (30m-primary/1m-calib) ===")
    print(f"baseline cell params: {CANDIDATES['current_default_blocked']}\n")

    in_sample_results = {}
    for name, params in CANDIDATES.items():
        if name == "current_default_blocked":
            continue
        book_c = build_book(params)
        res = run_set(IN_SAMPLE_DAYS, book_c, baseline_book, vix)
        in_sample_results[name] = res
        s = res["stats"]
        print(
            f"[IS] {name:24s} trades={res['total_cand_trades']:4d} "
            f"n_days={s['n']:2d} mean={s['mean']} std={s['std']} t={s['t']} p={s['p']} "
            f"params={params}"
        )

    print("\nWriting IS raw results...")
    out_path = "reports/research/channel_lab/tmf_30m_night_normal_cell_tune_is.json"
    with open(out_path, "w") as f:
        json.dump(in_sample_results, f, indent=2, ensure_ascii=False)
    print(f"Wrote {out_path}")

    # pick best in-sample candidate by mean delta among those with adequate
    # sample size (>=15 trade-appearances across the 22 IS days), else flag
    # insufficient data.
    eligible = {
        k: v for k, v in in_sample_results.items()
        if v["total_cand_trades"] >= 15
    }
    if not eligible:
        print("\n=== VERDICT: INSUFFICIENT_DATA (no candidate reaches 15 trades across 22 IS days) ===")
        max_trades_name = max(in_sample_results, key=lambda k: in_sample_results[k]["total_cand_trades"])
        print(f"closest: {max_trades_name} with {in_sample_results[max_trades_name]['total_cand_trades']} trades")
        return

    best_name = max(eligible, key=lambda k: eligible[k]["stats"]["mean"] or -1e18)
    best_stats = eligible[best_name]["stats"]
    print(f"\nBest IS candidate: {best_name} mean={best_stats['mean']} p={best_stats['p']}")

    if (best_stats["mean"] or 0) <= 0:
        print("=== VERDICT: NO_IMPROVEMENT (best candidate's mean delta <= 0 vs current default) ===")
        return

    # red-flag check: exclude largest-|delta| day
    per_day = eligible[best_name]["per_day"]
    if len(per_day) >= 2:
        biggest = max(per_day, key=lambda r: abs(r["delta"]))
        rest = [r["delta"] for r in per_day if r["day"] != biggest["day"]]
        rest_stats = paired_stats(rest)
        print(
            f"Excl. largest-|delta| day ({biggest['day']}, delta={biggest['delta']}): "
            f"mean={rest_stats['mean']} t={rest_stats['t']} p={rest_stats['p']} (n={rest_stats['n']})"
        )

    # OOS validation of the single best candidate
    print(f"\n=== OOS validation: {best_name} on {len(OOS_DAYS)} days ===")
    book_c = build_book(CANDIDATES[best_name])
    oos_res = run_set(OOS_DAYS, book_c, baseline_book, vix)
    oos_stats = oos_res["stats"]
    print(
        f"[OOS] {best_name:24s} trades={oos_res['total_cand_trades']:4d} "
        f"n_days={oos_stats['n']:2d} mean={oos_stats['mean']} std={oos_stats['std']} "
        f"t={oos_stats['t']} p={oos_stats['p']}"
    )

    out_path2 = "reports/research/channel_lab/tmf_30m_night_normal_cell_tune_oos.json"
    with open(out_path2, "w") as f:
        json.dump(dict(best_name=best_name, params=CANDIDATES[best_name],
                        in_sample=eligible[best_name], oos=oos_res), f, indent=2, ensure_ascii=False)
    print(f"Wrote {out_path2}")

    if (oos_stats["mean"] or 0) <= 0 or oos_res["total_cand_trades"] < 15:
        print("=== VERDICT: OOS_FAILED (OOS mean delta <= 0 or too few OOS trades) ===")
    else:
        print("=== VERDICT: ADOPT ===")


if __name__ == "__main__":
    main()
