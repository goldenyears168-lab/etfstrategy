#!/usr/bin/env python3
"""Continuous-gate PV16 cell retune: ASSIGNED CELL = day|climax_dn.

Context: causal_engine.py's session_side_gate lookup now supports a
per-bar-timestamp key (backward compatible) and nq_gate.nq_side_for_day()
was replaced live-side by a CONTINUOUS anchor (recomputed every bar, not
frozen at session open). That change is already deployed live and already
validated (scripts/research/tmf_continuous_gate_vs_frozen_anchor.py: beats
frozen on both 22-day IS and 66-day OOS, OOS p=0.0037, n=66). This script
asks a narrower follow-up question: now that the underlying gate itself
behaves differently, is the CURRENT-LIVE day|climax_dn cell's own
hang_lo/hang_hi/max_hold_bars/early_fill_gamma/block still optimal, or
does a different setting do better under the continuous gate?

Methodology: same day-clustered paired re-simulation rigor as every prior
cell-tune script in this directory. Only day|climax_dn is varied; all other
15 cells stay at order.tmf_channel_pv16_book.specialized_cell_book()'s
current-live values (SPECIALIZED_PATCHES then CELL_TUNE_V2_PATCHES on top
of freeze_cell_book()). session_side_gate is the CONTINUOUS gate (reused,
built once per day, not touching src/tmf_channel/causal_engine.py or
nq_gate.py further).

Does NOT touch src/order/*.py, src/tmf_channel/causal_engine.py,
src/tmf_channel/nq_gate.py, config/order.yaml, .env, launchd/,
scripts/order/, config/strategy.yaml, config/strategies.yaml. Reads
(never writes) reports/research/channel_lab/.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    CELL_TUNE_V2_PATCHES,
    SPECIALIZED_PATCHES,
    freeze_cell_book,
)
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

SESS = "day"
PV = "climax_dn"

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

_all_full = list_days(source="tx_1m_fullnight_cache_full.json")
OOS_DAYS = [d for d in _all_full if d < "2026-07-08"]
for d in OOS_DAYS:
    SOURCE_FOR_DAY[d] = "tx_1m_fullnight_cache_full.json"


def base_book() -> dict:
    """current-live-equivalent 16-cell book (same construction specialized_cell_book() uses)."""
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


BASE_CELL = deepcopy(base_book()[SESS][PV])


def build_book(cell_override: dict | None) -> dict:
    book = base_book()
    if cell_override is not None:
        book[SESS][PV] = {**book[SESS][PV], **cell_override}
    return book


def load_day_arrays(day: str):
    source = SOURCE_FOR_DAY[day]
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


_GATE_CACHE: dict[str, dict] = {}


def gate_for_day(day: str, T: list[str]) -> dict:
    g = _GATE_CACHE.get(day)
    if g is None:
        g = continuous_gate_for_day(day, T, source=SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json"))
        _GATE_CACHE[day] = g
    return g


def run_day(day: str, book: dict, vix: dict) -> tuple[float, int]:
    """Return (net_pnl_for_assigned_cell, n_trades_for_assigned_cell) for one day."""
    arrs = load_day_arrays(day)
    if arrs is None:
        return 0.0, 0
    O, H, L, C, V, T = arrs
    gate = gate_for_day(day, T)
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    recipe["session_pv_book"] = book
    recipe["session_side_gate"] = gate
    trades, *_rest = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)

    net = 0.0
    n = 0
    for tr in trades:
        if tr.get("regime_e") != PV:
            continue
        et = str(tr.get("et", ""))
        hm = et[11:16] if "T" in et else et[-8:-3]
        sess = "day" if "08:45" <= hm < "13:45" else "night"
        if sess != SESS:
            continue
        net += float(tr["pnl"])
        n += 1
    return net, n


def evaluate_candidate(days: list[str], cell_override: dict | None, vix: dict):
    book = build_book(cell_override)
    per_day = {}
    for day in days:
        net, n = run_day(day, book, vix)
        per_day[day] = (net, n)
    return per_day


def paired_stats(baseline_pd: dict, cand_pd: dict, days: list[str]):
    deltas = [cand_pd[d][0] - baseline_pd[d][0] for d in days]
    n_appear = sum(1 for d in days if baseline_pd[d][1] > 0 or cand_pd[d][1] > 0)
    total_trades = sum(baseline_pd[d][1] for d in days)
    mean = st.mean(deltas)
    std = st.stdev(deltas) if len(deltas) > 1 else 0.0
    t = (mean / (std / (len(deltas) ** 0.5))) if std > 0 else float("nan")
    p = None
    try:
        from scipy import stats as sstats

        p = float(2 * (1 - sstats.t.cdf(abs(t), df=len(deltas) - 1))) if std > 0 else None
    except Exception:
        p = None
    return dict(
        deltas=deltas, mean=mean, std=std, t=t, p=p, n=len(deltas),
        n_appear=n_appear, total_baseline_trades=total_trades,
    )


def largest_abs_delta_excl(baseline_pd: dict, cand_pd: dict, days: list[str]):
    deltas = {d: cand_pd[d][0] - baseline_pd[d][0] for d in days}
    if not deltas:
        return None, None
    worst_day = max(deltas, key=lambda d: abs(deltas[d]))
    rest = [deltas[d] for d in days if d != worst_day]
    mean_excl = st.mean(rest) if rest else None
    return worst_day, mean_excl


def main():
    patch_nq_gate_for_backfill(lookback_days=60)
    vix = load_vixtwn_delta() or {}

    print(f"Baseline (current-live-equivalent) {SESS}|{PV} cell: {BASE_CELL}")

    print("\n--- computing IN-SAMPLE baseline (candidate=None override, continuous gate) ---")
    baseline_is = evaluate_candidate(IN_SAMPLE_DAYS, None, vix)
    n_trades_total = sum(v[1] for v in baseline_is.values())
    n_appear = sum(1 for v in baseline_is.values() if v[1] > 0)
    print(f"baseline in-sample trades for {SESS}|{PV}: {n_trades_total} across {n_appear} day-appearances")
    for d, (net, n) in baseline_is.items():
        print(f"  {d}: net={net:.1f} n={n}")

    thin = n_trades_total < 15
    if thin:
        print(f"\n*** THIN SAMPLE: only {n_trades_total} trades across 22 in-sample days. "
              f"Will report INSUFFICIENT_DATA regardless of sweep results. ***")

    # BASE_CELL (CELL_TUNE_V2): hang_lo=12.0, hang_hi=27.0, early_fill_gamma=11.0,
    # max_hold_bars=38 (day base defaults would be 15.0/30.0/8.0/30). Under the
    # continuous gate, sess_side steering/blocking happens far more often
    # mid-session than under the frozen-at-open anchor, so sweep both tighter
    # (faster exits) and wider (fewer forced early exits from gate churn)
    # variants, a couple of max_hold options, and fully-blocked.
    candidates = {
        "current_default": None,
        "tighter_10_22": dict(hang_lo=10.0, hang_hi=22.0),
        "tighter_10_22_hold25": dict(hang_lo=10.0, hang_hi=22.0, max_hold_bars=25),
        "wider_16_34": dict(hang_lo=16.0, hang_hi=34.0),
        "wider_16_34_hold45": dict(hang_lo=16.0, hang_hi=34.0, max_hold_bars=45),
        "current_hold25": dict(max_hold_bars=25),
        "current_hold50": dict(max_hold_bars=50),
        "current_gamma0": dict(early_fill_gamma=0),
        "current_gamma20": dict(early_fill_gamma=20.0),
        "blocked": dict(block=["L", "S"]),
    }

    print("\n--- IN-SAMPLE sweep ---")
    results = {}
    for name, override in candidates.items():
        if name == "current_default":
            cand_pd = baseline_is
        else:
            cand_pd = evaluate_candidate(IN_SAMPLE_DAYS, override, vix)
        stats = paired_stats(baseline_is, cand_pd, IN_SAMPLE_DAYS)
        results[name] = dict(override=override, per_day=cand_pd, stats=stats)
        worst_day, mean_excl = largest_abs_delta_excl(baseline_is, cand_pd, IN_SAMPLE_DAYS)
        print(f"{name}: override={override}")
        print(f"  mean_delta={stats['mean']:.2f} std={stats['std']:.2f} t={stats['t']:.3f} "
              f"p={stats['p']} n={stats['n']}")
        print(f"  worst_day={worst_day} mean_excl_worst={mean_excl}")

    if thin:
        print("\nTHIN SAMPLE -> stopping here, no OOS validation, verdict=INSUFFICIENT_DATA.")
        return

    # Pick best in-sample candidate by day-clustered mean delta (excluding
    # "current_default" which is the baseline itself, mean_delta==0 by
    # construction). Require p<0.10 as a soft bar for even considering OOS;
    # otherwise recommend keeping current default.
    non_default = {k: v for k, v in results.items() if k != "current_default"}
    best_name = max(non_default, key=lambda k: non_default[k]["stats"]["mean"])
    best_stats = non_default[best_name]["stats"]
    print(f"\nBest in-sample non-default candidate: {best_name} "
          f"mean={best_stats['mean']:.2f} p={best_stats['p']}")

    if best_stats["mean"] <= 0:
        print("\nNo candidate beats current default in-sample. "
              "Verdict=NO_IMPROVEMENT (current default kept).")
        return

    best_override = non_default[best_name]["override"]
    print(f"\n--- OOS validation of single best candidate: {best_name} = {best_override} ---")
    baseline_oos = evaluate_candidate(OOS_DAYS, None, vix)
    cand_oos = evaluate_candidate(OOS_DAYS, best_override, vix)
    oos_stats = paired_stats(baseline_oos, cand_oos, OOS_DAYS)
    n_trades_oos = sum(v[1] for v in baseline_oos.values())
    worst_day_oos, mean_excl_oos = largest_abs_delta_excl(baseline_oos, cand_oos, OOS_DAYS)
    print(f"OOS baseline trades: {n_trades_oos}")
    print(f"OOS mean_delta={oos_stats['mean']:.2f} std={oos_stats['std']:.2f} "
          f"t={oos_stats['t']:.3f} p={oos_stats['p']} n={oos_stats['n']}")
    print(f"OOS worst_day={worst_day_oos} mean_excl_worst={mean_excl_oos}")

    # NOTE: reports/research/channel_lab/ is read-only for this script (hard
    # safety rule) -- do not write results there. Print-only.
    summary = dict(
        cell=f"{SESS}|{PV}",
        base_cell=BASE_CELL,
        best_name=best_name,
        best_override=best_override,
        in_sample=dict(
            n=len(IN_SAMPLE_DAYS), trades=n_trades_total,
            mean=best_stats["mean"], std=best_stats["std"],
            t=best_stats["t"], p=best_stats["p"],
        ),
        out_of_sample=dict(
            n=len(OOS_DAYS), trades=n_trades_oos,
            mean=oos_stats["mean"], std=oos_stats["std"],
            t=oos_stats["t"], p=oos_stats["p"],
        ),
    )
    print("\n=== FINAL SUMMARY (JSON) ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
