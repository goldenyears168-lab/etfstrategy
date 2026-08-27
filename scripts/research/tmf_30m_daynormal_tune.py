#!/usr/bin/env python3
"""Cell-tune pass for day|normal under the 30-min-primary / 1-min-calib PV8
architecture (see scripts/research/tmf_30m_primary_1m_calib_prototype.py for
the prototype this reuses verbatim -- build_pv30_series/patched_classify_pv_
factory are imported from there, unmodified).

Scope: ONLY day|normal's hang_lo/hang_hi/early_fill_gamma/max_hold_bars/
block are varied. All other 15 cells stay at the current-live-equivalent
default (order.tmf_channel_pv16_book.specialized_cell_book()) throughout.

Does NOT touch src/tmf_channel/causal_engine.py, src/order/, config/order.yaml,
.env, launchd/, scripts/order/. Read-only imports from order.* (config/recipe
constants only).
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
from tmf_30m_primary_1m_calib_prototype import (  # noqa: E402
    build_pv30_series,
    patched_classify_pv_factory,
)

_ORIG_CLASSIFY_PV = ce.classify_pv

SESS = "day"
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

OOS_SOURCE = "tx_1m_fullnight_cache_full.json"
ALL_OOS = [d for d in list_days(source=OOS_SOURCE) if d < "2026-07-08"]

BASE_BOOK = specialized_cell_book()
CURRENT_DEFAULT = dict(BASE_BOOK[SESS][PV])  # hang_lo=12, hang_hi=27, gamma=13, max_hold=38

CANDIDATES: dict[str, dict] = {
    "current_default": dict(CURRENT_DEFAULT),
    "wider_med": {**CURRENT_DEFAULT, "hang_lo": 15.0, "hang_hi": 35.0},
    "wider_big": {**CURRENT_DEFAULT, "hang_lo": 20.0, "hang_hi": 45.0},
    "cur_band_longhold": {**CURRENT_DEFAULT, "max_hold_bars": 50},
    "wider_med_longhold": {**CURRENT_DEFAULT, "hang_lo": 15.0, "hang_hi": 35.0, "max_hold_bars": 50},
    "wider_big_longhold_lowgamma": {
        **CURRENT_DEFAULT, "hang_lo": 20.0, "hang_hi": 45.0,
        "max_hold_bars": 50, "early_fill_gamma": 10.0,
    },
    "tighter_fast": {**CURRENT_DEFAULT, "hang_lo": 10.0, "hang_hi": 22.0, "early_fill_gamma": 15.0, "max_hold_bars": 25},
    "wider_med_shorthold": {**CURRENT_DEFAULT, "hang_lo": 18.0, "hang_hi": 38.0, "max_hold_bars": 25},
    "blocked": {**CURRENT_DEFAULT, "block": ["L", "S"]},
}


def build_book(candidate: dict) -> dict:
    book = deepcopy(BASE_BOOK)
    book[SESS][PV] = dict(candidate)
    return book


def day_arrays(day: str, source: str):
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


def is_day_session(hhmm: str) -> bool:
    return "08:45" <= hhmm < "13:45"


def run_book_for_day(day: str, source: str, recipe: dict, vix: dict, book: dict) -> float:
    """Returns net pnl of trades in cell (SESS, PV) per instructions' own
    session bucketing (entry HH:MM in [08:45,13:45) == day)."""
    arrs = day_arrays(day, source)
    if arrs is None:
        return 0.0
    O, H, L, C, V, T = arrs
    pv_series = build_pv30_series(T, O, H, L, C, V)
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    recipe_run = dict(recipe)
    recipe_run["session_pv_book"] = book
    try:
        trades, *_ = ce.simulate(O, H, L, C, V, T, recipe_run, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV
    net = 0.0
    for tr in trades:
        if tr.get("regime_e") != PV:
            continue
        hhmm = str(tr.get("et") or "")[11:16]
        sess = "day" if is_day_session(hhmm) else "night"
        if sess != SESS:
            continue
        net += float(tr["pnl"])
    return round(net, 1)


def ttest(deltas: list[float]):
    n = len(deltas)
    mean = st.mean(deltas)
    sd = st.stdev(deltas) if n > 1 else 0.0
    t = mean / (sd / (n ** 0.5)) if sd > 0 else float("nan")
    # crude two-sided p via normal approx (n>=20 in both windows here)
    from math import erf, sqrt
    p = float("nan")
    if sd > 0:
        z = abs(t)
        p = 2.0 * (1.0 - 0.5 * (1.0 + erf(z / sqrt(2))))
    return dict(n=n, mean=round(mean, 2), std=round(sd, 2), t=round(t, 3) if t == t else None,
                p=round(p, 4) if p == p else None)


def evaluate(days: list[str], candidate_book: dict, tag: str):
    baseline_book = BASE_BOOK
    per_day = []
    n_appearances = 0
    for day in days:
        source = SOURCE_FOR_DAY.get(day, OOS_SOURCE)
        base_net = run_book_for_day(day, source, PAPER_RECIPE, VIX, baseline_book)
        cand_net = run_book_for_day(day, source, PAPER_RECIPE, VIX, candidate_book)
        delta = round(cand_net - base_net, 1)
        per_day.append(dict(day=day, base_net=base_net, cand_net=cand_net, delta=delta))
        if base_net != 0.0 or cand_net != 0.0:
            n_appearances += 1
    deltas = [r["delta"] for r in per_day]
    stats = ttest(deltas)
    # red-flag check: exclude largest-|delta| day
    if len(deltas) > 1:
        idx = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
        rest = deltas[:idx] + deltas[idx + 1:]
        rest_mean = round(st.mean(rest), 2) if rest else None
    else:
        rest_mean = None
    return dict(
        tag=tag, per_day=per_day, stats=stats,
        n_appearances=n_appearances,
        largest_day=per_day[max(range(len(deltas)), key=lambda i: abs(deltas[i]))]["day"] if deltas else None,
        mean_excl_largest=rest_mean,
    )


def main():
    global VIX
    VIX = load_vixtwn_delta() or {}

    print(f"current_default recipe: {CURRENT_DEFAULT}")
    print(f"in-sample days: {len(IN_SAMPLE_DAYS)}  OOS days: {len(ALL_OOS)}")

    is_results = {}
    for tag, cand in CANDIDATES.items():
        if tag == "current_default":
            continue
        book = build_book(cand)
        res = evaluate(IN_SAMPLE_DAYS, book, tag)
        is_results[tag] = res
        s = res["stats"]
        print(
            f"[IN-SAMPLE] {tag:28s} n_app={res['n_appearances']:2d} "
            f"mean={s['mean']:8.2f} std={s['std']:8.2f} t={s['t']} p={s['p']} "
            f"largest_day={res['largest_day']} mean_excl_largest={res['mean_excl_largest']}"
        )

    # NOTE: reports/research/channel_lab/ is read-only for this task (hard
    # safety rule) -- write scratch output elsewhere.
    out_path = "/tmp/tmf_30m_daynormal_tune_insample.json"
    with open(out_path, "w") as f:
        json.dump(is_results, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
