#!/usr/bin/env python3
"""Retune day|climax_up cell params under the NEW continuous NQ/ES gate
(2026-08-10) — see scripts/research/tmf_continuous_gate_vs_frozen_anchor.py
for the gate change itself (already validated + deployed live, not
re-derived here).

Assigned cell: day|climax_up ONLY. All other 15 cells stay at the current
live specialized_cell_book() default throughout (both candidate and
baseline recipes use the identical book except for this one cell).

Current live default for day|climax_up (order.tmf_channel_pv16_book,
CELL_TUNE_V2_PATCHES applied on top of freeze_cell_book()):
    hang_lo=27.0, hang_hi=42.0, early_fill_gamma=0, max_hold_bars=30,
    block=[]

Methodology: true re-simulation via tmf_channel.engine.simulate() with the
CONTINUOUS session_side_gate (continuous_gate_for_day, imported not
rebuilt) injected on BOTH sides of every comparison — only the day|climax_up
cell recipe varies. Day-clustered paired comparison (one net-pnl-delta per
day, candidate minus baseline), across the 22-day IS window; then OOS
validation of the single best candidate only, across the 66-day window
(2026-04-01..2026-07-07).

Does NOT touch src/order/, src/tmf_channel/causal_engine.py,
src/tmf_channel/nq_gate.py, config/order.yaml, .env, launchd/,
scripts/order/, config/strategy.yaml, config/strategies.yaml.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import (  # noqa: E402
    AUG_DAYS,
    JULY_DAYS,
    SOURCE_FOR_DAY,
    continuous_gate_for_day,
)
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

SESS = "day"
PV = "climax_up"
IS_DAYS = JULY_DAYS + AUG_DAYS
DEFAULT_CELL = dict(
    hang_lo=27.0, hang_hi=42.0, early_fill_gamma=0, max_hold_bars=30, block=[],
)

try:
    from scipy import stats as sp

    def p_value(t_stat: float, n: int) -> float | None:
        return float(2 * (1 - sp.t.cdf(abs(t_stat), df=n - 1))) if n > 1 else None
except Exception:
    def p_value(t_stat: float, n: int) -> float | None:
        return None


def build_book(cell_overrides: dict) -> dict:
    """specialized_cell_book() with ONLY day|climax_up overridden."""
    book = specialized_cell_book()
    cell = dict(book[SESS][PV])
    cell.update(cell_overrides)
    book = deepcopy(book)
    book[SESS][PV] = cell
    return book


def _session_of(entry_hhmm: str) -> str:
    return "day" if "08:45" <= entry_hhmm < "13:45" else "night"


def cell_trades(trades: list[dict]) -> list[dict]:
    out = []
    for t in trades:
        if t.get("regime_e") != PV:
            continue
        et = t.get("et")
        if not et:
            continue
        hhmm = et.split("T", 1)[1][:5] if "T" in et else et[:5]
        if _session_of(hhmm) != SESS:
            continue
        out.append(t)
    return out


def run_day(day: str, candidate_book: dict, baseline_book: dict, vix: dict) -> dict:
    source = SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json")
    rows = load_day(day, source=source)
    if not rows:
        return dict(day=day, skipped=True)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)

    gate = continuous_gate_for_day(day, T, source=source)

    r_cand = deepcopy(PAPER_RECIPE)
    r_cand["session_pv_book"] = candidate_book
    r_cand["session_side_gate"] = gate

    r_base = deepcopy(PAPER_RECIPE)
    r_base["session_pv_book"] = baseline_book
    r_base["session_side_gate"] = gate

    trades_cand, *_ = simulate(O, H, L, C, V, T, r_cand, vix_delta=vix)
    trades_base, *_ = simulate(O, H, L, C, V, T, r_base, vix_delta=vix)

    cell_cand = cell_trades(trades_cand)
    cell_base = cell_trades(trades_base)
    net_cand = round(sum(t["pnl"] for t in cell_cand), 1)
    net_base = round(sum(t["pnl"] for t in cell_base), 1)

    return dict(
        day=day, n_cand=len(cell_cand), net_cand=net_cand,
        n_base=len(cell_base), net_base=net_base,
        diff=round(net_cand - net_base, 1),
    )


def day_clustered_stats(rows: list[dict]) -> dict:
    diffs = [r["diff"] for r in rows]
    n = len(diffs)
    if n == 0:
        return dict(n=0, mean=None, std=None, t=None, p=None)
    mean_d = st.mean(diffs)
    std_d = st.stdev(diffs) if n > 1 else 0.0
    t_stat = mean_d / (std_d / (n ** 0.5)) if std_d > 0 else 0.0
    return dict(n=n, mean=mean_d, std=std_d, t=t_stat, p=p_value(t_stat, n))


def largest_day_excluded_check(rows: list[dict]) -> dict:
    if not rows:
        return {}
    biggest = max(rows, key=lambda r: abs(r["diff"]))
    rest = [r for r in rows if r["day"] != biggest["day"]]
    stats_rest = day_clustered_stats(rest)
    return dict(excluded_day=biggest["day"], excluded_diff=biggest["diff"], stats_without=stats_rest)


def total_cell_appearances(rows: list[dict]) -> int:
    return sum(r.get("n_base", 0) for r in rows)


def main():
    patch_nq_gate_for_backfill(lookback_days=60)
    vix = load_vixtwn_delta() or {}

    baseline_book = specialized_cell_book()

    # Candidate combos: (hang_lo, hang_hi, max_hold_bars, early_fill_gamma, block)
    candidates = {
        "default": dict(DEFAULT_CELL),
        "wider_band": dict(hang_lo=20.0, hang_hi=50.0, max_hold_bars=30, early_fill_gamma=0, block=[]),
        "narrower_band": dict(hang_lo=32.0, hang_hi=38.0, max_hold_bars=30, early_fill_gamma=0, block=[]),
        "shift_lo": dict(hang_lo=20.0, hang_hi=42.0, max_hold_bars=30, early_fill_gamma=0, block=[]),
        "shorter_hold": dict(hang_lo=27.0, hang_hi=42.0, max_hold_bars=15, early_fill_gamma=0, block=[]),
        "longer_hold": dict(hang_lo=27.0, hang_hi=42.0, max_hold_bars=45, early_fill_gamma=0, block=[]),
        "gamma_on": dict(hang_lo=27.0, hang_hi=42.0, max_hold_bars=30, early_fill_gamma=8.0, block=[]),
        "blocked": dict(hang_lo=27.0, hang_hi=42.0, max_hold_bars=30, early_fill_gamma=0, block=["L", "S"]),
    }

    is_results = {}
    for name, cell in candidates.items():
        cand_book = build_book(cell)
        rows = []
        for day in IS_DAYS:
            r = run_day(day, cand_book, baseline_book, vix)
            if r.get("skipped"):
                continue
            rows.append(r)
        stats = day_clustered_stats(rows)
        is_results[name] = dict(cell=cell, rows=rows, stats=stats)
        print(f"[IS] {name}: n={stats['n']} mean={stats['mean']} std={stats['std']} "
              f"t={stats['t']} p={stats['p']} appearances={total_cell_appearances(rows)}",
              flush=True)

    out = dict(is_results={
        k: dict(cell=v["cell"], stats=v["stats"],
                largest_excl=largest_day_excluded_check(v["rows"]),
                appearances=total_cell_appearances(v["rows"]))
        for k, v in is_results.items()
    })

    best_name = None
    best_mean = None
    for name, v in is_results.items():
        if name == "default":
            continue
        s = v["stats"]
        if s["n"] == 0 or s["mean"] is None:
            continue
        if best_mean is None or s["mean"] > best_mean:
            best_mean = s["mean"]
            best_name = name

    print("\n=== IN-SAMPLE SUMMARY ===")
    for name, v in is_results.items():
        s = v["stats"]
        print(f"{name}: {v['cell']} mean={s['mean']} t={s['t']} p={s['p']} n={s['n']}")
    print(f"\nbest non-default by mean: {best_name} (mean={best_mean})")

    total_appearances = total_cell_appearances(is_results["default"]["rows"])
    print(f"total day|climax_up trade-appearances across {len(IS_DAYS)} IS days: {total_appearances}")

    out["best_candidate_name"] = best_name
    out["total_is_appearances"] = total_appearances

    with open("/tmp/tmf_cell_tune_day_climax_up_is.json", "w") as f:
        json.dump(out, f, indent=2, default=str)

    if total_appearances < 15:
        print("\nINSUFFICIENT DATA: fewer than ~15 trade-appearances in-sample; stopping before OOS.")
        return

    if best_name is None or is_results[best_name]["stats"]["mean"] <= is_results["default"]["stats"]["mean"]:
        print("\nNo candidate beats default in-sample; recommend keeping current default. Skipping OOS run "
              "(nothing to validate).")
        return

    # OOS validation of the single best candidate only.
    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]
    best_cell = is_results[best_name]["cell"]
    cand_book = build_book(best_cell)
    oos_rows = []
    for day in oos_days:
        r = run_day(day, cand_book, baseline_book, vix)
        if r.get("skipped"):
            continue
        oos_rows.append(r)
    oos_stats = day_clustered_stats(oos_rows)
    oos_appearances = total_cell_appearances(oos_rows)
    print(f"\n=== OOS ({len(oos_days)} days requested, {len(oos_rows)} available) for {best_name} ===")
    print(f"cell={best_cell}")
    print(f"mean={oos_stats['mean']} std={oos_stats['std']} t={oos_stats['t']} p={oos_stats['p']} "
          f"n={oos_stats['n']} appearances={oos_appearances}")
    print(f"largest-day-excluded check: {largest_day_excluded_check(oos_rows)}")

    out["oos"] = dict(name=best_name, cell=best_cell, stats=oos_stats,
                       appearances=oos_appearances,
                       largest_excl=largest_day_excluded_check(oos_rows))
    with open("/tmp/tmf_cell_tune_day_climax_up_result.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("\nWrote /tmp/tmf_cell_tune_day_climax_up_result.json")


if __name__ == "__main__":
    main()
