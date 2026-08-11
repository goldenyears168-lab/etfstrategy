#!/usr/bin/env python3
"""Combine the 16 independent cell-retune verdicts (continuous NQ/ES gate,
2026-08-10 campaign) into ONE final book and true-re-simulate it against the
current-live baseline (specialized_cell_book(), unmodified) -- both legs get
the SAME continuous session_side_gate injected, so this isolates ONLY the
effect of the parameter changes, not the already-deployed gate change.

Only one of the 16 cells returned ADOPT: night|expand_dn (unblock,
hang_lo=16, hang_hi=30, early_fill_gamma=5, max_hold_bars=16). All other 15
cells keep their current-live default (specialized_cell_book()'s own value)
untouched -- NO_IMPROVEMENT / OOS_FAILED / INSUFFICIENT_DATA verdicts do not
change anything.

Does NOT touch src/tmf_channel/causal_engine.py, src/tmf_channel/nq_gate.py,
src/order/*.py, config/order.yaml, .env, launchd/, scripts/order/,
config/strategy.yaml, config/strategies.yaml. Only reads
reports/research/channel_lab/ (writes ONE new result file there, per task
instructions -- the only exception to read-only).
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy
from math import erf, sqrt
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

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

ADOPTED_OVERRIDES = {
    ("night", "expand_dn"): dict(
        block=[], hang_lo=16.0, hang_hi=30.0,
        early_fill_gamma=5.0, max_hold_bars=16,
    ),
}

CELL_VERDICTS = {
    "day|climax_up": "INSUFFICIENT_DATA",
    "day|climax_dn": "INSUFFICIENT_DATA",
    "day|expand_up": "OOS_FAILED",
    "day|expand_dn": "INSUFFICIENT_DATA",
    "day|contract": "NO_IMPROVEMENT",
    "day|dry": "INSUFFICIENT_DATA",
    "day|normal": "NO_IMPROVEMENT",
    "day|div_hh_weak_vol": "OOS_FAILED",
    "night|climax_up": "INSUFFICIENT_DATA",
    "night|climax_dn": "OOS_FAILED",
    "night|expand_up": "OOS_FAILED",
    "night|expand_dn": "ADOPT",
    "night|contract": "OOS_FAILED",
    "night|dry": "OOS_FAILED",
    "night|normal": "NO_IMPROVEMENT",
    "night|div_hh_weak_vol": "NO_IMPROVEMENT",
}


def load_arrays(day: str, source_map: dict):
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


def build_final_book():
    book = deepcopy(specialized_cell_book())
    for (sess, pv), overrides in ADOPTED_OVERRIDES.items():
        book[sess][pv].update(overrides)
    return book


def run_book_full(arrays, gate: dict, book, recipe_base: dict, vix: dict):
    O, H, L, C, V, T = arrays
    recipe = deepcopy(recipe_base)
    recipe["session_pv_book"] = book
    recipe["session_side_gate"] = gate
    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    net = sum(float(tr["pnl"]) for tr in trades)
    return net, len(trades)


def paired_stats(deltas: list[float]):
    n = len(deltas)
    if n < 2:
        return dict(n=n, mean=deltas[0] if deltas else 0.0, std=0.0, t=0.0, p=1.0)
    mean = st.mean(deltas)
    sd = st.stdev(deltas)
    t = 0.0 if sd == 0 else mean / (sd / (n ** 0.5))
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))
    return dict(n=n, mean=mean, std=sd, t=t, p=p)


def one_sample_stats(vals: list[float]):
    """Descriptive day-clustered stats for a single series (not a delta)."""
    n = len(vals)
    if n < 2:
        return dict(n=n, mean=vals[0] if vals else 0.0, std=0.0)
    return dict(n=n, mean=st.mean(vals), std=st.stdev(vals))


def evaluate_window(days: list[str], source_map: dict, recipe_base: dict,
                     vix: dict, baseline_book, final_book):
    base_net_by_day = {}
    final_net_by_day = {}
    base_n_by_day = {}
    final_n_by_day = {}
    for d in days:
        arr = load_arrays(d, source_map)
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source=SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json"))
        bnet, bn = run_book_full(arr, gate, baseline_book, recipe_base, vix)
        fnet, fn = run_book_full(arr, gate, final_book, recipe_base, vix)
        base_net_by_day[d] = bnet
        final_net_by_day[d] = fnet
        base_n_by_day[d] = bn
        final_n_by_day[d] = fn
    return base_net_by_day, final_net_by_day, base_n_by_day, final_n_by_day


def artifact_check(deltas_by_day: dict[str, float]):
    if len(deltas_by_day) < 2:
        return dict(top_day=None, top_delta=None, mean_excl_top=None)
    items = list(deltas_by_day.items())
    top_day, top_delta = max(items, key=lambda kv: abs(kv[1]))
    rest = [v for k, v in items if k != top_day]
    mean_excl = st.mean(rest) if rest else None
    return dict(top_day=top_day, top_delta=top_delta, mean_excl_top=mean_excl)


def main():
    out_path = Path("reports/research/channel_lab/tmf_continuous_gate_16cell_retune_result.json")

    patch_nq_gate_for_backfill(lookback_days=60)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")

    baseline_book = deepcopy(specialized_cell_book())
    final_book = build_final_book()

    print("=== IN-SAMPLE (22 days) ===")
    b_net_is, f_net_is, b_n_is, f_n_is = evaluate_window(
        IN_SAMPLE_DAYS, SOURCE_FOR_DAY, recipe_base, vix, baseline_book, final_book)
    delta_is = {d: f_net_is[d] - b_net_is[d] for d in b_net_is}
    is_stats = paired_stats(list(delta_is.values()))
    is_artifact = artifact_check(delta_is)
    is_base_trades = sum(b_n_is.values())
    is_final_trades = sum(f_n_is.values())
    n_is_days = len(b_net_is)
    print(f"IS n_days={is_stats['n']} mean_delta={is_stats['mean']:.2f} "
          f"std={is_stats['std']:.2f} t={is_stats['t']:.2f} p={is_stats['p']:.4f}")
    print(f"IS baseline_trades/day={is_base_trades/n_is_days:.2f} "
          f"final_trades/day={is_final_trades/n_is_days:.2f}")
    print(f"IS single-day-artifact: top_day={is_artifact['top_day']} "
          f"top_delta={is_artifact['top_delta']} mean_excl_top={is_artifact['mean_excl_top']}")

    print("\n=== OUT-OF-SAMPLE (66 days) ===")
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json")
                if d < "2026-07-08"]
    oos_source_map = {d: "tx_1m_fullnight_cache_full.json" for d in oos_days}
    b_net_oos, f_net_oos, b_n_oos, f_n_oos = evaluate_window(
        oos_days, oos_source_map, recipe_base, vix, baseline_book, final_book)
    delta_oos = {d: f_net_oos[d] - b_net_oos[d] for d in b_net_oos}
    oos_stats = paired_stats(list(delta_oos.values()))
    oos_artifact = artifact_check(delta_oos)
    oos_base_trades = sum(b_n_oos.values())
    oos_final_trades = sum(f_n_oos.values())
    n_oos_days = len(b_net_oos)
    print(f"OOS n_days={oos_stats['n']} mean_delta={oos_stats['mean']:.2f} "
          f"std={oos_stats['std']:.2f} t={oos_stats['t']:.2f} p={oos_stats['p']:.4f}")
    print(f"OOS baseline_trades/day={oos_base_trades/n_oos_days:.2f} "
          f"final_trades/day={oos_final_trades/n_oos_days:.2f}")
    print(f"OOS single-day-artifact: top_day={oos_artifact['top_day']} "
          f"top_delta={oos_artifact['top_delta']} mean_excl_top={oos_artifact['mean_excl_top']}")

    result = dict(
        cell_verdicts=CELL_VERDICTS,
        adopted_overrides={f"{s}|{p}": v for (s, p), v in ADOPTED_OVERRIDES.items()},
        final_book=final_book,
        baseline_book=baseline_book,
        in_sample_stats=dict(
            n_days=n_is_days,
            baseline_net_pnl=one_sample_stats(list(b_net_is.values())),
            final_net_pnl=one_sample_stats(list(f_net_is.values())),
        ),
        oos_stats=dict(
            n_days=n_oos_days,
            baseline_net_pnl=one_sample_stats(list(b_net_oos.values())),
            final_net_pnl=one_sample_stats(list(f_net_oos.values())),
        ),
        vs_live_baseline_in_sample=dict(
            **is_stats, sum_delta=sum(delta_is.values()),
            single_day_artifact=is_artifact,
            per_day_delta=delta_is,
        ),
        vs_live_baseline_oos=dict(
            **oos_stats, sum_delta=sum(delta_oos.values()),
            single_day_artifact=oos_artifact,
            per_day_delta=delta_oos,
        ),
        trade_count_comparison=dict(
            in_sample=dict(
                baseline_total=is_base_trades, final_total=is_final_trades,
                baseline_per_day=is_base_trades / n_is_days,
                final_per_day=is_final_trades / n_is_days,
            ),
            out_of_sample=dict(
                baseline_total=oos_base_trades, final_total=oos_final_trades,
                baseline_per_day=oos_base_trades / n_oos_days,
                final_per_day=oos_final_trades / n_oos_days,
            ),
        ),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
