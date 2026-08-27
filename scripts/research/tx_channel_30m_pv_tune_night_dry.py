#!/usr/bin/env python3
"""Cell-tune (2026-08-09): night|dry under 30-min-primary/1-min-calib PV8
regime feed. Assigned cell: night|dry.

Architecture under test (already prototyped, NOT built here): PV8 regime
classification is driven by 30-minute bars (updates only every 30 min,
using the last FULLY CLOSED 30-min bucket -- PIT safe), while all 1-min
execution mechanics (hang levels, fills, exits, struct_break, trail, stop,
max_hold) stay exactly as they are today. See
scripts/research/tmf_30m_primary_1m_calib_prototype.py for the mechanism
(build_pv30_series / patched_classify_pv_factory monkeypatch pattern,
copied verbatim below).

This script varies ONLY the night|dry cell's hang_lo/hang_hi/max_hold_bars/
early_fill_gamma/block. All other 15 cells stay at the current-live-
equivalent default (order.tmf_channel_pv16_book.specialized_cell_book()).

Does NOT touch src/order/*.py, src/tmf_channel/causal_engine.py,
config/order.yaml, .env, launchd/, scripts/order/, config/strategy.yaml,
config/strategies.yaml.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from collections import defaultdict
from copy import deepcopy

sys.path.insert(0, "src")

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402

SESS = "night"
PV = "dry"
CELL = f"{SESS}|{PV}"

_ORIG_CLASSIFY_PV = ce.classify_pv
_ORIG_RVOL_SERIES = ce.rvol_series

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

BIN_MIN = 30

DRY_30M = 0.357
CONTRACT_30M = 0.586
EXPAND_30M = 2.118
CLIMAX_30M = 4.510


def _bucket_key(hm: str) -> str:
    h, m = int(hm[:2]), int(hm[3:5])
    m30 = (m // BIN_MIN) * BIN_MIN
    return f"{h:02d}:{m30:02d}"


def classify_pv_30m(C, O, rvol, t, look=5):
    if rvol[t] is None or t < 1:
        return "na", 0.0
    rv = rvol[t]
    a = max(0, t - look)
    impulse = C[t] - C[a]
    up = impulse > 0
    if rv >= CLIMAX_30M:
        return ("climax_up" if up else "climax_dn"), impulse
    if rv >= EXPAND_30M:
        return ("expand_up" if up else "expand_dn"), impulse
    if rv <= DRY_30M:
        return "dry", impulse
    if rv <= CONTRACT_30M:
        return "contract", impulse
    hh = C[t] >= max(C[a : t + 1]) - 1e-9
    if hh and rv < 1.0 and impulse > 0:
        return "div_hh_weak_vol", impulse
    return "normal", impulse


def build_pv30_series(T, O, H, L, C, V):
    n = len(T)
    hm = [t[11:16] for t in T]
    bucket_of = [_bucket_key(h) for h in hm]

    buckets: list[list[int]] = []
    cur_key = None
    for i in range(n):
        if bucket_of[i] != cur_key:
            buckets.append([])
            cur_key = bucket_of[i]
        buckets[-1].append(i)

    O30 = [O[idxs[0]] for idxs in buckets]
    H30 = [max(H[i] for i in idxs) for idxs in buckets]
    L30 = [min(L[i] for i in idxs) for idxs in buckets]
    C30 = [C[idxs[-1]] for idxs in buckets]
    V30 = [sum(V[i] for i in idxs) for idxs in buckets]

    rv30 = _ORIG_RVOL_SERIES(V30)
    pv30 = []
    for bi in range(len(buckets)):
        reg, _ = classify_pv_30m(C30, O30, rv30, bi)
        pv30.append(reg)

    out = ["na"] * n
    for b_idx, idxs in enumerate(buckets):
        prior_pv = pv30[b_idx - 1] if b_idx > 0 else "na"
        for i in idxs:
            out[i] = prior_pv
    return out


def patched_classify_pv_factory(pv_series):
    def _patched(C, O, rvol, t, look=5):
        return pv_series[t], 0.0
    return _patched


def build_book(cell_override: dict | None) -> dict:
    """Current-live-equivalent 16-cell book, with night|dry replaced by
    cell_override (if given)."""
    book = specialized_cell_book()
    if cell_override is not None:
        book[SESS][PV] = dict(cell_override)
    return book


def entry_session(trade) -> str:
    hm = str(trade.get("et") or "")[11:16]
    return "day" if "08:45" <= hm < "13:45" else "night"


def run_day(day: str, book: dict, vix: dict) -> dict:
    source = SOURCE_FOR_DAY.get(day, OOS_SOURCE)
    rows = load_day(day, source=source)
    if not rows:
        return dict(day=day, n_trades=0, sum_pnl=0.0, cell_n=0, cell_pnl=0.0, skipped=True)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]

    pv_series = build_pv30_series(T, O, H, L, C, V)
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    try:
        recipe = deepcopy(PAPER_RECIPE)
        recipe.setdefault("hang_anchor", "O")
        recipe["session_pv_book"] = book
        trades, events, ws, wl, rvol, regime, open_pos = ce.simulate(
            O, H, L, C, V, T, recipe, vix_delta=vix
        )
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV

    cell_trades = [
        tr for tr in trades
        if tr.get("regime_e") == PV and entry_session(tr) == SESS
    ]
    return dict(
        day=day,
        n_trades=len(trades),
        sum_pnl=round(sum(t["pnl"] for t in trades), 1),
        cell_n=len(cell_trades),
        cell_pnl=round(sum(t["pnl"] for t in cell_trades), 1),
    )


def paired_stats(deltas: list[float]) -> dict:
    n = len(deltas)
    if n < 2:
        return dict(n=n, mean=None, std=None, t=None, p=None)
    mean = st.mean(deltas)
    sd = st.stdev(deltas)
    if sd == 0:
        t_stat = float("inf") if mean != 0 else 0.0
        p = 0.0 if mean != 0 else 1.0
    else:
        from scipy import stats as sps

        t_stat, p = sps.ttest_1samp(deltas, 0.0)
        t_stat = float(t_stat)
        p = float(p)
    return dict(n=n, mean=round(mean, 2), std=round(sd, 2), t=round(t_stat, 3), p=round(p, 4))


def run_set(days: list[str], book: dict, vix: dict) -> list[dict]:
    return [run_day(d, book, vix) for d in days]


def cell_delta_series(base_results: list[dict], cand_results: list[dict]) -> list[float]:
    b = {r["day"]: r for r in base_results}
    c = {r["day"]: r for r in cand_results}
    days = [d for d in b if not b[d].get("skipped") and not c[d].get("skipped")]
    return [c[d]["cell_pnl"] - b[d]["cell_pnl"] for d in days], days


def main():
    vix = load_vixtwn_delta() or {}
    base_book = build_book(None)
    baseline_cell = dict(base_book[SESS][PV])
    print(f"=== Cell {CELL} — baseline params: {baseline_cell} ===\n")

    print("--- Baseline (current-live-equivalent book) on in-sample days ---")
    base_results = run_set(IN_SAMPLE_DAYS, base_book, vix)
    for r in base_results:
        print(json.dumps(r, ensure_ascii=False))
    total_cell_n = sum(r["cell_n"] for r in base_results)
    total_cell_pnl = sum(r["cell_pnl"] for r in base_results)
    n_days_present = sum(1 for r in base_results if r["cell_n"] > 0)
    print(
        f"baseline: total {CELL} trades={total_cell_n} across {n_days_present}/"
        f"{len(IN_SAMPLE_DAYS)} days, sum_pnl={total_cell_pnl:.1f}\n"
    )

    candidates = [
        dict(name="c1_narrow_baseline", hang_lo=14.0, hang_hi=28.0, max_hold_bars=20, early_fill_gamma=5.0),
        dict(name="c2_wide", hang_lo=20.0, hang_hi=40.0, max_hold_bars=20, early_fill_gamma=5.0),
        dict(name="c3_wide_longhold", hang_lo=20.0, hang_hi=40.0, max_hold_bars=40, early_fill_gamma=5.0),
        dict(name="c4_verywide_longhold", hang_lo=26.0, hang_hi=50.0, max_hold_bars=45, early_fill_gamma=5.0),
        dict(name="c5_wide_shorthold", hang_lo=20.0, hang_hi=40.0, max_hold_bars=12, early_fill_gamma=5.0),
        dict(name="c6_wide_zerogamma", hang_lo=20.0, hang_hi=40.0, max_hold_bars=20, early_fill_gamma=0.0),
        dict(name="c7_moderate", hang_lo=17.0, hang_hi=34.0, max_hold_bars=28, early_fill_gamma=5.0),
        dict(name="c8_blocked", block=["L", "S"]),
    ]

    cand_stats = {}
    all_results = {}
    for cand in candidates:
        name = cand["name"]
        override = dict(baseline_cell)
        for k, v in cand.items():
            if k == "name":
                continue
            override[k] = v
        book = build_book(override)
        results = run_set(IN_SAMPLE_DAYS, book, vix)
        all_results[name] = results
        deltas, days_used = cell_delta_series(base_results, results)
        stats = paired_stats(deltas)
        cand_stats[name] = dict(override=override, stats=stats, deltas=list(zip(days_used, deltas)))
        print(f"--- candidate {name}: {override} ---")
        print(f"  in-sample paired stats vs baseline: {stats}")
        if deltas:
            biggest = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
            print(f"  biggest-|delta| day: {days_used[biggest]} delta={deltas[biggest]:.1f}")
            rest = deltas[:biggest] + deltas[biggest + 1:]
            if rest:
                print(f"  mean excluding that day: {st.mean(rest):.2f}")
        print()

    total_appearances = n_days_present
    print(f"=== night|dry cell appears on {total_appearances}/{len(IN_SAMPLE_DAYS)} in-sample days, "
          f"{total_cell_n} total trades ===\n")

    out = dict(
        cell=CELL,
        baseline=baseline_cell,
        baseline_in_sample=dict(total_cell_n=total_cell_n, total_cell_pnl=total_cell_pnl,
                                 n_days_present=n_days_present),
        candidates={k: dict(override=v["override"], stats=v["stats"]) for k, v in cand_stats.items()},
    )
    out_path = "/tmp/tx_channel_30m_pv_tune_night_dry_insample.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
