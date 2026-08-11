#!/usr/bin/env python3
"""Independent candidate search: day|expand_up (currently block=["L","S"])
and day|expand_dn (currently block=["L"], i.e. S-side live) small hang_lo/
hang_hi grid retune. Portfolio-level (whole-book, not standalone-cell) paired
day-clustered significance vs the unchanged specialized_cell_book() baseline.

Read-only research. Does not touch src/tmf_channel/causal_engine.py or any
live src/order/ file.
"""
from __future__ import annotations

import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

from scipy import stats as sp

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
OOS_SOURCE = "tx_1m_fullnight_cache_full.json"


def load_arrays(day: str):
    source = SOURCE_FOR_DAY.get(day, OOS_SOURCE)
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


def build_cache(days):
    cache = {}
    for d in days:
        arr = load_arrays(d)
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source=SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json"))
        cache[d] = (arr, gate)
    return cache


def portfolio_net(arr, gate, book, recipe_base, vix):
    O, H, L, C, V, T = arr
    recipe = deepcopy(recipe_base)
    recipe["session_pv_book"] = book
    recipe["session_side_gate"] = gate
    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    return sum(float(t["pnl"]) for t in trades), trades


def cell_net(trades, sess, pv):
    def sess_of(hm):
        return "day" if "08:45" <= hm < "13:45" else "night"
    net, n = 0.0, 0
    for t in trades:
        if t.get("regime_e") != pv:
            continue
        hm = str(t.get("et") or "")
        hm = hm.split("T", 1)[1][:5] if "T" in hm else hm[:5]
        if sess_of(hm) != sess:
            continue
        net += float(t["pnl"])
        n += 1
    return net, n


def paired_stats(deltas):
    n = len(deltas)
    if n < 2:
        return dict(n=n, mean=(deltas[0] if deltas else 0.0), std=0.0, t=0.0, p=None)
    mean = st.mean(deltas)
    sd = st.stdev(deltas)
    t_stat = mean / (sd / (n ** 0.5)) if sd > 0 else 0.0
    p = float(2 * (1 - sp.t.cdf(abs(t_stat), df=n - 1))) if sd > 0 else 1.0
    return dict(n=n, mean=mean, std=sd, t=t_stat, p=p)


def excl_top_day(deltas):
    if len(deltas) < 2:
        return None
    i_max = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
    rest = deltas[:i_max] + deltas[i_max + 1:]
    return st.mean(rest) if rest else 0.0


def scan_window(days_cache, sess, pv, overrides, recipe_base, vix, baseline_net_cache):
    deltas = []
    cell_nets = []
    for d, (arr, gate) in days_cache.items():
        book = deepcopy(specialized_cell_book())
        book[sess][pv].update(overrides)
        cnet, trades = portfolio_net(arr, gate, book, recipe_base, vix)
        bnet = baseline_net_cache[d]
        deltas.append(cnet - bnet)
        cn, _ = cell_net(trades, sess, pv)
        cell_nets.append(cn)
    return deltas, cell_nets


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    is_cache = build_cache(IS_DAYS)
    oos_days = [d for d in list_days(source=OOS_SOURCE) if d < "2026-07-08"]
    oos_cache = build_cache(oos_days)

    baseline_book = deepcopy(specialized_cell_book())
    is_baseline_net = {}
    for d, (arr, gate) in is_cache.items():
        net, _ = portfolio_net(arr, gate, baseline_book, recipe_base, vix)
        is_baseline_net[d] = net
    oos_baseline_net = {}
    for d, (arr, gate) in oos_cache.items():
        net, _ = portfolio_net(arr, gate, baseline_book, recipe_base, vix)
        oos_baseline_net[d] = net

    GRID = [(hl, hh) for hl in (12.0, 16.0, 20.0) for hh in (28.0, 32.0, 38.0)]

    TARGETS = [
        ("day", "expand_up", {"block": ["S"]}, "L-only unblock, current block=['L','S']"),
        ("day", "expand_dn", {"block": ["L"]}, "S-only, already live, retune"),
    ]

    for sess, pv, fixed_overrides, note in TARGETS:
        cur = specialized_cell_book()[sess][pv]
        print(f"\n### {sess}|{pv} ({note}) current live params: {cur}")
        print(f"grid: hang_lo x hang_hi over {GRID}, early_fill_gamma="
              f"{cur['early_fill_gamma']}, max_hold_bars={cur['max_hold_bars']} (fixed)")

        results = []
        for hl, hh in GRID:
            ov = dict(fixed_overrides)
            ov["hang_lo"] = hl
            ov["hang_hi"] = hh
            ov["early_fill_gamma"] = cur["early_fill_gamma"]
            ov["max_hold_bars"] = cur["max_hold_bars"]
            deltas, cell_nets = scan_window(is_cache, sess, pv, ov, recipe_base, vix, is_baseline_net)
            stats = paired_stats(deltas)
            excl = excl_top_day(deltas)
            results.append((hl, hh, stats, excl, sum(cell_nets)))
            print(f"  hang_lo={hl:5.1f} hang_hi={hh:5.1f}  portfolio: n={stats['n']:2d} "
                  f"mean={stats['mean']:7.2f} t={stats['t']:6.2f} p={stats['p']}"
                  f"  excl_top_day={excl if excl is None else round(excl,2)}"
                  f"  standalone_cell_sum={sum(cell_nets):8.1f}")

        best = max(results, key=lambda r: r[2]["mean"])
        hl, hh, stats, excl, cell_sum = best
        print(f"  -> IS best: hang_lo={hl} hang_hi={hh} mean={stats['mean']:.2f} "
              f"t={stats['t']:.2f} p={stats['p']}")

        promising = (stats["mean"] > 0 and stats["p"] is not None and stats["p"] < 0.10)
        if not promising:
            print(f"  IS NOT promising for {sess}|{pv} (best mean={stats['mean']:.2f}, "
                  f"p={stats['p']}) -> skip OOS, report negative.")
            continue

        ov = dict(fixed_overrides)
        ov["hang_lo"] = hl
        ov["hang_hi"] = hh
        ov["early_fill_gamma"] = cur["early_fill_gamma"]
        ov["max_hold_bars"] = cur["max_hold_bars"]
        oos_deltas, oos_cell_nets = scan_window(oos_cache, sess, pv, ov, recipe_base, vix, oos_baseline_net)
        oos_stats = paired_stats(oos_deltas)
        oos_excl = excl_top_day(oos_deltas)
        print(f"  OOS ({oos_stats['n']} days): mean={oos_stats['mean']:.2f} "
              f"t={oos_stats['t']:.2f} p={oos_stats['p']} "
              f"excl_top_day={oos_excl if oos_excl is None else round(oos_excl,2)} "
              f"standalone_cell_sum={sum(oos_cell_nets):.1f}")

        same_dir = (stats["mean"] > 0) == (oos_stats["mean"] > 0)
        print(f"  IS/OOS same direction: {same_dir}")


if __name__ == "__main__":
    main()
