#!/usr/bin/env python3
"""Cell-tune (2026-08-09): night|div_hh_weak_vol under the 30m-primary /
1m-calib PV8 architecture (see tmf_30m_primary_1m_calib_prototype.py).

Assigned cell ONLY: night|div_hh_weak_vol. All other 15 cells stay exactly
at the current-live-equivalent default (order.tmf_channel_pv16_book
specialized_cell_book(): freeze_cell_book() + SPECIALIZED_PATCHES +
CELL_TUNE_V2_PATCHES). Under that book, night|div_hh_weak_vol is currently
block=["L","S"] (fully blocked, zero trades) -- so "current default" for
this cell is a hard block; candidates test whether unblocking it (with
various hang/max_hold/gamma) is worth it once PV8 classification comes
from stable 30-min buckets instead of flickery 1-min bars.

Methodology: day-clustered candidate-minus-baseline total-day-pnl delta,
paired t-test, across the 22-day in-sample window; single final candidate
validated once against the 66-day OOS window (< 2026-07-08). Never touches
src/order/*.py, src/tmf_channel/causal_engine.py, config/order.yaml, .env,
launchd/, scripts/order/, config/strategy.yaml, config/strategies.yaml.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    CELL_TUNE_V2_PATCHES,
    SPECIALIZED_PATCHES,
    freeze_cell_book,
)
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402

sys.path.insert(0, "scripts/research")
from tmf_30m_primary_1m_calib_prototype import (  # noqa: E402
    build_pv30_series,
    patched_classify_pv_factory,
)

_ORIG_CLASSIFY_PV = ce.classify_pv

MY_SESS = "night"
MY_PV = "div_hh_weak_vol"

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})
IN_SAMPLE_DAYS = JULY_DAYS + AUG_DAYS

OOS_SOURCE = "tx_1m_fullnight_cache_full.json"
OOS_DAYS = [d for d in list_days(source=OOS_SOURCE) if d < "2026-07-08"]


def current_live_equivalent_book():
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


BASELINE_BOOK = current_live_equivalent_book()
assert BASELINE_BOOK[MY_SESS][MY_PV].get("block") == ["L", "S"], (
    "expected current-live night|div_hh_weak_vol to be block=['L','S']"
)


def make_book(overrides: dict) -> dict:
    book = deepcopy(BASELINE_BOOK)
    book[MY_SESS][MY_PV].update(overrides)
    return book


def is_day_session(hhmm: str) -> bool:
    return "08:45" <= hhmm < "13:45"


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


def run_day(day: str, source: str, book: dict, recipe_base: dict, vix: dict) -> dict:
    arrs = load_arrays(day, source)
    if arrs is None:
        return dict(day=day, skipped=True, sum_pnl=0.0, n_trades=0, my_cell_trades=[])
    O, H, L, C, V, T = arrs
    pv_series = build_pv30_series(T, O, H, L, C, V)
    recipe = deepcopy(recipe_base)
    recipe["session_pv_book"] = book
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    try:
        trades, events, ws, wl, rvol, regime, open_pos = ce.simulate(
            O, H, L, C, V, T, recipe, vix_delta=vix
        )
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV

    my_cell = []
    for tr in trades:
        if tr.get("regime_e") != MY_PV:
            continue
        hhmm = str(tr.get("et") or "")
        if "T" in hhmm:
            hhmm = hhmm.split("T", 1)[1][:5]
        else:
            hhmm = hhmm[:5]
        sess = "day" if is_day_session(hhmm) else "night"
        if sess == MY_SESS:
            my_cell.append(tr)

    return dict(
        day=day,
        skipped=False,
        sum_pnl=round(sum(t["pnl"] for t in trades), 1),
        n_trades=len(trades),
        my_cell_n=len(my_cell),
        my_cell_pnl=round(sum(t["pnl"] for t in my_cell), 1),
    )


def paired_stats(deltas: list[float]) -> dict:
    n = len(deltas)
    mean = st.mean(deltas) if n else 0.0
    sd = st.stdev(deltas) if n > 1 else 0.0
    t = (mean / (sd / (n ** 0.5))) if (n > 1 and sd > 0) else 0.0
    # two-sided p via normal approx (n modest, t-dist close enough for n~20; report t)
    from math import erf, sqrt
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2)))) if n > 1 else 1.0
    return dict(n=n, mean=round(mean, 2), std=round(sd, 2), t=round(t, 3), p=round(p, 4))


def evaluate_book(book: dict, days: list[str], source_for_day, recipe_base, vix, label: str):
    baseline_results = {}
    cand_results = {}
    for day in days:
        source = source_for_day(day)
        baseline_results[day] = run_day(day, source, BASELINE_BOOK, recipe_base, vix)
        cand_results[day] = run_day(day, source, book, recipe_base, vix)

    deltas = []
    my_cell_n_total = 0
    my_cell_pnl_total = 0.0
    per_day = []
    for day in days:
        b = baseline_results[day]
        c = cand_results[day]
        if b.get("skipped") or c.get("skipped"):
            continue
        delta = c["sum_pnl"] - b["sum_pnl"]
        deltas.append(delta)
        my_cell_n_total += c["my_cell_n"]
        my_cell_pnl_total += c["my_cell_pnl"]
        per_day.append(dict(day=day, delta=round(delta, 1), my_cell_n=c["my_cell_n"],
                             my_cell_pnl=c["my_cell_pnl"]))

    stats = paired_stats(deltas)
    # red-flag check: exclude largest |delta| day
    if len(deltas) > 1:
        idx_max = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
        rest = deltas[:idx_max] + deltas[idx_max + 1:]
        stats_ex = paired_stats(rest)
    else:
        stats_ex = None

    print(f"\n=== {label} ===")
    for row in per_day:
        print(json.dumps(row))
    print(f"stats={stats}")
    if stats_ex:
        print(f"stats_excl_largest_|delta|_day={stats_ex}")
    print(f"my_cell_total_trades={my_cell_n_total} my_cell_total_pnl={round(my_cell_pnl_total, 1)}")
    return dict(stats=stats, stats_excl_largest=stats_ex, my_cell_n=my_cell_n_total,
                my_cell_pnl=round(my_cell_pnl_total, 1), per_day=per_day)


def main():
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    candidates = {
        "unblock_default_18_32_g5_mh20": dict(
            block=[], hang_lo=18.0, hang_hi=32.0, early_fill_gamma=5.0, max_hold_bars=20,
        ),
        "unblock_wide_22_38_g5_mh20": dict(
            block=[], hang_lo=22.0, hang_hi=38.0, early_fill_gamma=5.0, max_hold_bars=20,
        ),
        "unblock_wider_26_42_g5_mh20": dict(
            block=[], hang_lo=26.0, hang_hi=42.0, early_fill_gamma=5.0, max_hold_bars=20,
        ),
        "unblock_wide_22_38_g8_mh20": dict(
            block=[], hang_lo=22.0, hang_hi=38.0, early_fill_gamma=8.0, max_hold_bars=20,
        ),
        "unblock_wide_22_38_g5_mh30": dict(
            block=[], hang_lo=22.0, hang_hi=38.0, early_fill_gamma=5.0, max_hold_bars=30,
        ),
        "unblock_wide_22_38_g5_mh14": dict(
            block=[], hang_lo=22.0, hang_hi=38.0, early_fill_gamma=5.0, max_hold_bars=14,
        ),
        "stay_blocked_LS": dict(block=["L", "S"]),
    }

    source_for_day_in = lambda d: SOURCE_FOR_DAY[d]  # noqa: E731
    source_for_day_oos = lambda d: OOS_SOURCE  # noqa: E731

    print("BASELINE current-live-equivalent night|div_hh_weak_vol cell:",
          json.dumps(BASELINE_BOOK[MY_SESS][MY_PV]))

    is_results = {}
    for name, overrides in candidates.items():
        book = make_book(overrides)
        is_results[name] = evaluate_book(
            book, IN_SAMPLE_DAYS, source_for_day_in, recipe_base, vix,
            label=f"IN-SAMPLE candidate={name} overrides={overrides}",
        )

    # NOTE: reports/research/channel_lab/ is read-only for this campaign
    # (per task constraints) -- write scratch output to /tmp instead.
    out_path = "/tmp/tmf_30m_cell_tune_night_div_hh_weak_vol_insample.json"
    with open(out_path, "w") as f:
        json.dump(
            {name: {"overrides": candidates[name], **{k: v for k, v in r.items() if k != "per_day"},
                     "per_day": r["per_day"]}
             for name, r in is_results.items()},
            f, indent=2, ensure_ascii=False,
        )
    print(f"\nWrote {out_path}")

    print("\n\n############ IN-SAMPLE SUMMARY ############")
    for name, r in is_results.items():
        print(f"{name}: mean={r['stats']['mean']} std={r['stats']['std']} "
              f"t={r['stats']['t']} p={r['stats']['p']} n={r['stats']['n']} "
              f"cell_trades={r['my_cell_n']} cell_pnl={r['my_cell_pnl']}")


if __name__ == "__main__":
    main()
