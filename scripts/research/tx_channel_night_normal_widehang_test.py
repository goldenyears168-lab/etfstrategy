#!/usr/bin/env python3
"""night|normal wide-hang unblock test (parallel cell assignment, 2026-08-08).

Hypothesis under test: night|normal is currently hard-blocked (block=["L","S"])
in the live PAPER_RECIPE (final_v1_4_0_pv16_normal_dhwv_block) because the cell
is net-loss on average. Question: does placing the resting entry MUCH farther
from anchor (so only rare extreme touches fill) produce a different, possibly
positive, expected value for those rare fills -- even though the cell's normal
average is negative?

True re-simulation only (per METHODOLOGY RULES): deepcopy PAPER_RECIPE, for
each candidate set ONLY session_pv_book['night']['normal']['block']=[] and
hang_lo/hang_hi to the candidate values, call engine.simulate() per day across
all 4 sanctioned windows (w83, julsep25, octdec25, janmar26), then filter
trades to regime_e=='normal' AND entry-bar session=='night' (derived from the
entry timestamp using the exact same HH:MM>=15:00 or <05:00 rule the engine
itself uses for session tagging -- see causal_engine._hhmm / line ~1459).

Day-clustered stats: per trading day, sum PnL of matching trades (0 if none
that day), one-sample t-test across days with >=1 matching trade.
"""
from __future__ import annotations

import statistics as st
from copy import deepcopy
from math import erf, sqrt
from pathlib import Path

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate

OUT = Path("reports/research/channel_lab/tx_channel_night_normal_widehang_test.json")

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}

CANDIDATES = [
    {"label": "1.5x", "hang_lo": 27.0, "hang_hi": 48.0},
    {"label": "2x", "hang_lo": 36.0, "hang_hi": 64.0},
    {"label": "3x", "hang_lo": 54.0, "hang_hi": 96.0},
]

BASE_LO, BASE_HI = 18.0, 32.0  # night session base (unblocked-cell-style)


def _hhmm(ts: str) -> str:
    s = str(ts or "")
    if "T" in s:
        return s.split("T", 1)[1][:5]
    return s.split()[-1][:5]


def _is_night(ts: str) -> bool:
    hm = _hhmm(ts)
    return hm >= "15:00" or hm < "05:00"


def _arrays_from_rows(rows, day: str):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def make_recipe(hang_lo: float, hang_hi: float) -> dict:
    r = deepcopy(PAPER_RECIPE)
    r.setdefault("hang_anchor", "O")
    cell = r["session_pv_book"]["night"]["normal"]
    cell["block"] = []
    cell["hang_lo"] = hang_lo
    cell["hang_hi"] = hang_hi
    return r


def run_candidate(recipe: dict, vix: dict) -> dict:
    """Return per-window trade lists (each trade tagged with day) matching
    regime_e=='normal' AND session=='night'."""
    out = {}
    for label, cache in WINDOWS.items():
        days = list_days(cache)
        trades_by_day = {}
        for day in days:
            rows = load_day(day, source=cache)
            if not rows:
                continue
            O, H, L, C, V, T = _arrays_from_rows(rows, day)
            trades, events, ws, wl, rvol, regime, open_pos = simulate(
                O, H, L, C, V, T, recipe, vix_delta=vix
            )
            matched = [
                t
                for t in (trades or [])
                if t.get("regime_e") == "normal" and _is_night(t.get("et"))
            ]
            if matched:
                trades_by_day[day] = matched
        out[label] = trades_by_day
    return out


def day_clustered_stats(trades_by_day_all_windows: dict) -> dict:
    """trades_by_day_all_windows: {window: {day: [trade,...]}}"""
    day_sums = []
    all_trades = []
    for _label, dd in trades_by_day_all_windows.items():
        for _day, trs in dd.items():
            s = sum(float(t["pnl"]) for t in trs)
            day_sums.append(s)
            all_trades.extend(trs)
    n_days = len(day_sums)
    n_trades = len(all_trades)
    if n_days == 0:
        return {
            "n_days": 0, "n_trades": 0, "t": 0.0, "p": 1.0,
            "mean_pnl_per_trade": 0.0, "win_rate": 0.0, "mean_day_sum": 0.0,
        }
    mean = sum(day_sums) / n_days
    sd = st.stdev(day_sums) if n_days > 1 else 0.0
    if sd == 0:
        t = 0.0
    else:
        t = mean / (sd / sqrt(n_days))
    p = 2.0 * (1.0 - 0.5 * (1.0 + erf(abs(t) / sqrt(2)))) if n_days > 1 else 1.0
    pnls = [float(t["pnl"]) for t in all_trades]
    wins = sum(1 for x in pnls if x > 0)
    return {
        "n_days": n_days,
        "n_trades": n_trades,
        "t": round(t, 3),
        "p": round(p, 4),
        "mean_day_sum": round(mean, 2),
        "mean_pnl_per_trade": round(sum(pnls) / n_trades, 2) if n_trades else 0.0,
        "win_rate": round(100.0 * wins / n_trades, 1) if n_trades else 0.0,
        "sum_pnl": round(sum(pnls), 1),
    }


def per_window_breakdown(trades_by_day_all_windows: dict) -> dict:
    out = {}
    for label, dd in trades_by_day_all_windows.items():
        all_trades = [t for trs in dd.values() for t in trs]
        n = len(all_trades)
        pnls = [float(t["pnl"]) for t in all_trades]
        wins = sum(1 for x in pnls if x > 0)
        out[label] = {
            "n_days": len(dd),
            "n_trades": n,
            "sum_pnl": round(sum(pnls), 1) if pnls else 0.0,
            "mean_pnl": round(sum(pnls) / n, 2) if n else 0.0,
            "win_rate": round(100.0 * wins / n, 1) if n else 0.0,
        }
    return out


def single_trade_removal_check(trades_by_day_all_windows: dict) -> dict:
    all_trades = [
        t
        for dd in trades_by_day_all_windows.values()
        for trs in dd.values()
        for t in trs
    ]
    if not all_trades:
        return {"n_trades": 0}
    pnls = [float(t["pnl"]) for t in all_trades]
    total = sum(pnls)
    best_i = max(range(len(pnls)), key=lambda i: pnls[i])
    worst_i = min(range(len(pnls)), key=lambda i: pnls[i])
    return {
        "n_trades": len(pnls),
        "sum_pnl": round(total, 1),
        "best_trade_pnl": round(pnls[best_i], 1),
        "sum_excl_best": round(total - pnls[best_i], 1),
        "worst_trade_pnl": round(pnls[worst_i], 1),
        "sum_excl_worst": round(total - pnls[worst_i], 1),
    }


def main():
    print("Baseline night|normal cell:", PAPER_RECIPE["session_pv_book"]["night"]["normal"])
    vix = load_vixtwn_delta() or {}

    results = {"base_hang_lo": BASE_LO, "base_hang_hi": BASE_HI, "candidates": {}}

    for cand in CANDIDATES:
        label = cand["label"]
        recipe = make_recipe(cand["hang_lo"], cand["hang_hi"])
        print(f"\n=== candidate {label} (hang_lo={cand['hang_lo']}, hang_hi={cand['hang_hi']}) ===")
        tbd = run_candidate(recipe, vix)
        stats = day_clustered_stats(tbd)
        window_bd = per_window_breakdown(tbd)
        removal = single_trade_removal_check(tbd)
        print("STATS", stats)
        print("PER-WINDOW", window_bd)
        print("REMOVAL-CHECK", removal)
        results["candidates"][label] = {
            "hang_lo": cand["hang_lo"],
            "hang_hi": cand["hang_hi"],
            "stats": stats,
            "per_window": window_bd,
            "removal_check": removal,
        }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    import json

    OUT.write_text(json.dumps(results, indent=2))
    print("\nWROTE", OUT)


if __name__ == "__main__":
    main()
