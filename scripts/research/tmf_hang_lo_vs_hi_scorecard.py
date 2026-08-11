#!/usr/bin/env python3
"""Absolute performance scorecard (not just delta-vs-baseline) for the three
hang-anchor variants compared in tmf_hang_lo_vs_hi_anchor_compare.py:
current live structure-based pick, always_lo (inner), always_hi (outer).
Same IS/OOS windows, same continuous gate, same v1.5.0 cell book.
"""
from __future__ import annotations

import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

_ORIG_ABOVE = ce._pick_hang_above
_ORIG_BELOW = ce._pick_hang_below

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})
IS_DAYS = JULY_DAYS + AUG_DAYS


def _patch_always_lo():
    ce._pick_hang_above = lambda spot, levels, *, lo, hi, pad: spot + lo
    ce._pick_hang_below = lambda spot, levels, *, lo, hi, pad: spot - lo


def _patch_always_hi():
    ce._pick_hang_above = lambda spot, levels, *, lo, hi, pad: spot + hi
    ce._pick_hang_below = lambda spot, levels, *, lo, hi, pad: spot - hi


def _restore():
    ce._pick_hang_above = _ORIG_ABOVE
    ce._pick_hang_below = _ORIG_BELOW


def load_arrays(day, source_map):
    source = source_map.get(day, "tx_1m_fullnight_cache_full.json")
    rows = load_day(day, source=source)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)
    return O, H, L, C, V, T


def run_variant(days, source_map, recipe_base, vix, patch_fn):
    total_net = 0.0
    total_trades = 0
    wins = 0
    day_nets = []
    for d in days:
        arr = load_arrays(d, source_map)
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source=SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json"))
        recipe = deepcopy(recipe_base)
        recipe["session_side_gate"] = gate
        O, H, L, C, V, T = arr
        if patch_fn:
            patch_fn()
        try:
            trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        finally:
            _restore()
        net = sum(t["pnl"] for t in trades)
        total_net += net
        total_trades += len(trades)
        wins += sum(1 for t in trades if t["pnl"] > 0)
        day_nets.append(net)
    n_days = len(day_nets)
    win_rate = (wins / total_trades * 100) if total_trades else 0.0
    avg_trade_pnl = (total_net / total_trades) if total_trades else 0.0
    day_win_rate = (sum(1 for x in day_nets if x > 0) / n_days * 100) if n_days else 0.0
    return dict(
        n_days=n_days, total_trades=total_trades, total_net=round(total_net, 1),
        net_per_day=round(total_net / n_days, 2) if n_days else 0.0,
        trades_per_day=round(total_trades / n_days, 2) if n_days else 0.0,
        win_rate_pct=round(win_rate, 1), day_win_rate_pct=round(day_win_rate, 1),
        avg_trade_pnl=round(avg_trade_pnl, 2),
    )


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()

    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]
    oos_source_map = {d: "tx_1m_fullnight_cache_full.json" for d in oos_days}

    variants = [("baseline_smart_pick", None), ("always_lo", _patch_always_lo), ("always_hi", _patch_always_hi)]
    windows = [("IS_22d", IS_DAYS, SOURCE_FOR_DAY), ("OOS_66d", oos_days, oos_source_map)]

    print(f"{'window':10s} {'variant':20s} {'n_days':6s} {'trades':7s} {'trades/day':10s} "
          f"{'net_total':10s} {'net/day':9s} {'win%':6s} {'day_win%':9s} {'avg_trade':10s}")
    rows = {}
    for wlabel, days, smap in windows:
        for vname, pfn in variants:
            r = run_variant(days, smap, recipe_base, vix, pfn)
            rows[f"{wlabel}_{vname}"] = r
            print(f"{wlabel:10s} {vname:20s} {r['n_days']:<6d} {r['total_trades']:<7d} "
                  f"{r['trades_per_day']:<10.2f} {r['total_net']:<10.1f} {r['net_per_day']:<9.2f} "
                  f"{r['win_rate_pct']:<6.1f} {r['day_win_rate_pct']:<9.1f} {r['avg_trade_pnl']:<10.2f}")

    import json
    out_path = "reports/research/channel_lab/tmf_hang_lo_vs_hi_scorecard_result.json"
    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
