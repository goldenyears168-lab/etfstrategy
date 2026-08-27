#!/usr/bin/env python3
"""Order-layer-aware replay: current LIVE recipe vs LIVE + CELL_TUNE_V3_PATCHES
(day|normal + day|div_hh_weak_vol fully blocked), across the same 22-day
corrected full-day (00:00-23:59) window (2026-08-09).

CELL_TUNE_V3 was proposed 2026-08-07, evaluated and NOT adopted 2026-08-08
after a 5-round audit traced its original "p<0.001" backtest to a same-day
look-ahead bug in the research harness's NQ overnight-gate anchor (see
config/strategy.yaml applied_refinements, r_gate_anchor_v4_audit.json).
CELL_TUNE_V3_PATCHES is still defined (unused) in tmf_channel_pv16_book.py
for exactly this kind of re-evaluation.

This re-test is triggered by an INDEPENDENT signal, not a re-run of the old
evidence: a fresh 16-cell breakdown across this same 22-day window (raw
simulate(), no session_side_gate injection at all -- gate-independent)
found day|normal (411 trades, 41% of all volume) net -3651.0pt and
day|div_hh_weak_vol (67 trades) net -2020.0pt -- the two largest loss
centers of all 16 cells, independently pointing at the same two cells v3
proposed blocking. This script tests that idea with today's corrected
tools: order-layer-aware replay (not raw simulate()), full night-session
bars (no truncation), and the widened 60-day NQ gate lookback.

Does NOT touch src/order/, config/order.yaml, .env, launchd/, scripts/order/.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import CELL_TUNE_V3_PATCHES  # noqa: E402
from tmf_channel.cache_store import load_day  # noqa: E402
from tmf_order_layer_aware_replay import (  # noqa: E402
    patch_nq_gate_for_backfill,
    replay_day,
)

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]

SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})


def v3_book(live_book: dict) -> dict:
    book = deepcopy(live_book)
    for sess, reg, upd in CELL_TUNE_V3_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def run_one_day(day: str) -> dict | None:
    patch_nq_gate_for_backfill(lookback_days=60)

    live_recipe = deepcopy(PAPER_RECIPE)
    live_recipe.setdefault("hang_anchor", "O")

    v3_recipe = deepcopy(PAPER_RECIPE)
    v3_recipe["session_pv_book"] = v3_book(PAPER_RECIPE["session_pv_book"])
    v3_recipe.setdefault("hang_anchor", "O")

    source = SOURCE_FOR_DAY[day]
    rows = load_day(day, source=source)
    if not rows:
        return None
    r_live = replay_day(day, rows, live_recipe)
    r_v3 = replay_day(day, rows, v3_recipe)
    diff = r_v3["sum_pnl"] - r_live["sum_pnl"]
    return dict(
        day=day, live_n=r_live["n_trades"], live_pnl=r_live["sum_pnl"],
        v3_n=r_v3["n_trades"], v3_pnl=r_v3["sum_pnl"],
        diff=round(diff, 1),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None)
    args = ap.parse_args()

    if args.day:
        row = run_one_day(args.day)
        print(json.dumps(row) if row else json.dumps({"day": args.day, "error": "no rows"}))
        return

    rows_out = []
    for day in JULY_DAYS + AUG_DAYS:
        row = run_one_day(day)
        if row is None:
            print(f"{day}: no rows, skipped", flush=True)
            continue
        rows_out.append(row)
        print(json.dumps(row), flush=True)

    live_vals = [r["live_pnl"] for r in rows_out]
    v3_vals = [r["v3_pnl"] for r in rows_out]
    diffs = [r["diff"] for r in rows_out]
    n = len(rows_out)

    def stats(vals):
        return dict(
            n=len(vals), sum=round(sum(vals), 1),
            mean=round(st.mean(vals), 2) if vals else None,
            std=round(st.stdev(vals), 2) if len(vals) > 1 else None,
        )

    mean_diff = st.mean(diffs) if diffs else 0.0
    std_diff = st.stdev(diffs) if len(diffs) > 1 else 0.0
    t_stat = (mean_diff / (std_diff / (n ** 0.5))) if (n > 1 and std_diff > 0) else 0.0
    try:
        from scipy import stats as sp_stats

        p_val = float(2 * (1 - sp_stats.t.cdf(abs(t_stat), df=n - 1))) if n > 1 else None
    except Exception:
        p_val = None

    summary = dict(
        n_days=n,
        live=stats(live_vals),
        v3_block=stats(v3_vals),
        diff_v3_minus_live=dict(
            mean=round(mean_diff, 2), std=round(std_diff, 2),
            t=round(t_stat, 3), p=round(p_val, 4) if p_val is not None else None,
        ),
    )
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    out_path = "reports/research/channel_lab/tmf_replay_v3_block_vs_live_result.json"
    with open(out_path, "w") as f:
        json.dump(dict(rows=rows_out, summary=summary), f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
