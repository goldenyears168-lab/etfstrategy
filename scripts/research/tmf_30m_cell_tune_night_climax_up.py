#!/usr/bin/env python3
"""30m-primary/1m-calib PV16 cell tune — assigned cell: night|climax_up.

Architecture under test (see scripts/research/tmf_30m_primary_1m_calib_prototype.py
for the full mechanism writeup, already prototyped/validated there — this
script only reuses build_pv30_series/patched_classify_pv_factory from it):
PV8 regime classification driven by 30-minute bars (recalibrated thresholds),
while all execution mechanics (hang levels, fills, exits, struct_break,
trail, stop, max_hold) stay on 1-minute granularity exactly as today.

This script tunes ONLY the night|climax_up cell (hang_lo, hang_hi,
max_hold_bars, early_fill_gamma, whether block=["L","S"]) against the
CURRENT-live-equivalent 16-cell book (freeze_cell_book() + SPECIALIZED_PATCHES
+ CELL_TUNE_V2_PATCHES from order.tmf_channel_pv16_book, i.e.
specialized_cell_book()) as the baseline for all other 15 cells.

NOTE: in the current-live-equivalent book, night|climax_up is a HARD BLOCK
(block=["L","S"]) at both the freeze_cell_book() NIGHT_BLOCKS layer and
unmodified by any later patch layer — so the "current default" produces
ZERO trades in this cell by construction. The candidates below therefore
test whether UNBLOCKING this cell (with newly-tuned params, informed by the
coarser 30-min regime read) beats staying blocked.

Does NOT touch src/tmf_channel/causal_engine.py, src/order/, config/order.yaml,
.env, launchd/, scripts/order/. Reads (never writes) reports/research/channel_lab/.
"""
from __future__ import annotations

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
from tmf_30m_primary_1m_calib_prototype import (  # noqa: E402
    build_pv30_series,
    patched_classify_pv_factory,
)

_ORIG_CLASSIFY_PV = ce.classify_pv

SESS = "night"
PV = "climax_up"

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
IS_SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
IS_SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})
IS_DAYS = JULY_DAYS + AUG_DAYS

OOS_SOURCE = "tx_1m_fullnight_cache_full.json"
OOS_DAYS = [d for d in list_days(source=OOS_SOURCE) if d < "2026-07-08"]


def base_book() -> dict:
    return specialized_cell_book()


def build_book(cell_override: dict | None) -> dict:
    book = deepcopy(base_book())
    if cell_override is not None:
        book[SESS][PV] = dict(cell_override)
    return book


def in_session(hhmm: str) -> str:
    return "day" if "08:45" <= hhmm < "13:45" else "night"


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


def run_day(day: str, source: str, recipe_base: dict, vix: dict, cell_override: dict | None):
    arrs = load_arrays(day, source)
    if arrs is None:
        return None
    O, H, L, C, V, T = arrs
    pv_series = build_pv30_series(T, O, H, L, C, V)
    recipe = deepcopy(recipe_base)
    recipe["session_pv_book"] = build_book(cell_override)
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    try:
        trades, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV
    my_net = 0.0
    my_n = 0
    total_net = 0.0
    for tr in trades:
        total_net += float(tr["pnl"])
        et = tr.get("et") or ""
        hm = et[11:16] if "T" in et else None
        sess = in_session(hm) if hm else None
        if tr.get("regime_e") == PV and sess == SESS:
            my_net += float(tr["pnl"])
            my_n += 1
    return dict(day=day, total_net=round(total_net, 1), my_net=round(my_net, 1), my_n=my_n)


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
    # two-sided p from t via normal approx fallback if scipy absent
    try:
        from scipy import stats as sstats

        p = float(2 * (1 - sstats.t.cdf(abs(t), df=n - 1)))
    except Exception:
        # crude normal approximation
        import math

        p = float(2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))))
    return dict(n=n, mean=round(mean, 2), std=round(std, 2), t=round(t, 3), p=round(p, 5))


def evaluate_candidate(days: list[str], source_for_day, recipe_base, vix, cell_override, label):
    baseline_results = {}
    cand_results = {}
    for day in days:
        source = source_for_day(day)
        b = run_day(day, source, recipe_base, vix, None)
        c = run_day(day, source, recipe_base, vix, cell_override)
        if b is None or c is None:
            continue
        baseline_results[day] = b
        cand_results[day] = c
    deltas = []
    my_ns = []
    per_day = []
    for day in days:
        if day not in baseline_results:
            continue
        b = baseline_results[day]
        c = cand_results[day]
        delta = c["total_net"] - b["total_net"]
        deltas.append(delta)
        my_ns.append(c["my_n"])
        per_day.append((day, delta, c["my_n"], c["my_net"]))
    stats = paired_stats(deltas)
    total_my_n = sum(my_ns)
    print(f"--- {label} ---")
    print(f"n_days={len(deltas)} total_my_trades={total_my_n} stats={stats}")
    if deltas:
        # exclude largest-|delta| day sensitivity check
        idx_max = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
        excl = deltas[:idx_max] + deltas[idx_max + 1:]
        excl_stats = paired_stats(excl) if len(excl) >= 2 else dict(n=len(excl))
        print(
            f"  excl_largest_day={per_day[idx_max][0]} delta={per_day[idx_max][1]:.1f} "
            f"-> stats_without_it={excl_stats}"
        )
    return dict(label=label, stats=stats, total_my_n=total_my_n, per_day=per_day)


def main():
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    def is_source(day):
        return IS_SOURCE_FOR_DAY[day]

    def oos_source(day):
        return OOS_SOURCE

    # Candidate grid. Current default = block=["L","S"] (0 trades, delta=0
    # vs itself trivially) -- included implicitly as "no unblock" option.
    night_climax_base = dict(specialized_cell_book()["night"]["climax_up"])
    print("current default night|climax_up cell:", night_climax_base)

    candidates = {
        # narrow band, same order of magnitude as other night cells, short hold
        "unblock_narrow_h18_32_mh20_g5": dict(
            night_climax_base, block=[], hang_lo=18.0, hang_hi=32.0,
            max_hold_bars=20, early_fill_gamma=5.0,
        ),
        # wider band (30-min regime persists longer -> less frequent re-hang)
        "unblock_wide_h30_60_mh30_g5": dict(
            night_climax_base, block=[], hang_lo=30.0, hang_hi=60.0,
            max_hold_bars=30, early_fill_gamma=5.0,
        ),
        "unblock_wide_h35_70_mh40_g0": dict(
            night_climax_base, block=[], hang_lo=35.0, hang_hi=70.0,
            max_hold_bars=40, early_fill_gamma=0.0,
        ),
        "unblock_wide_h25_50_mh45_g5": dict(
            night_climax_base, block=[], hang_lo=25.0, hang_hi=50.0,
            max_hold_bars=45, early_fill_gamma=5.0,
        ),
        "unblock_verywide_h40_80_mh60_g5": dict(
            night_climax_base, block=[], hang_lo=40.0, hang_hi=80.0,
            max_hold_bars=60, early_fill_gamma=5.0,
        ),
        # aggressive early fill in wide band
        "unblock_wide_h30_60_mh30_g12": dict(
            night_climax_base, block=[], hang_lo=30.0, hang_hi=60.0,
            max_hold_bars=30, early_fill_gamma=12.0,
        ),
    }

    print("\n===== IN-SAMPLE (22 days) =====")
    is_summary = {}
    for label, cell in candidates.items():
        res = evaluate_candidate(IS_DAYS, is_source, recipe_base, vix, cell, label)
        is_summary[label] = res

    # rank by mean delta among those with adequate sample (>=15 trade-day-appearances)
    ranked = sorted(
        is_summary.items(),
        key=lambda kv: (kv[1]["stats"]["mean"] if kv[1]["stats"]["mean"] is not None else -1e18),
        reverse=True,
    )
    print("\n===== IN-SAMPLE RANKING (mean daily delta pts vs current-blocked default) =====")
    for label, res in ranked:
        print(f"{label}: mean={res['stats']['mean']} t={res['stats']['t']} p={res['stats']['p']} total_my_n={res['total_my_n']}")

    best_label, best_res = ranked[0]
    print(f"\nBest in-sample candidate: {best_label} total_my_n={best_res['total_my_n']}")

    if best_res["total_my_n"] < 15:
        print("INSUFFICIENT DATA: fewer than ~15 trade-appearances across 22 in-sample days for best candidate.")
        print("Recommend keeping current default (block=[\"L\",\"S\"]) -- do not force a tuned recommendation.")
        return

    if best_res["stats"]["mean"] is None or best_res["stats"]["mean"] <= 0:
        print("NO IMPROVEMENT: best candidate does not beat current-blocked default in-sample.")
        return

    print(f"\n===== OOS VALIDATION (66 days) for {best_label} =====")
    winning_cell = candidates[best_label]
    oos_res = evaluate_candidate(OOS_DAYS, oos_source, recipe_base, vix, winning_cell, best_label + "_OOS")

    print("\n===== FINAL SUMMARY =====")
    print("winning cell dict:", winning_cell)
    print("IN-SAMPLE:", best_res["stats"], "total_my_n=", best_res["total_my_n"])
    print("OOS:", oos_res["stats"], "total_my_n=", oos_res["total_my_n"])


if __name__ == "__main__":
    main()
