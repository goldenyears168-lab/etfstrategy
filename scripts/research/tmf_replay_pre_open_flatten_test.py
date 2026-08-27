#!/usr/bin/env python3
"""Test: does proactively flattening any overnight-carried position before
the day-session open (08:45) reduce gap-risk tail losses vs riding it and
relying on the 90-min wall-clock max-hold safety net?

Triggered by 2026-07-29 forensics: current live recipe and the v1.2.0
baseline both carried a position through an ~2400pt overnight gap into the
08:45 day-session open, neither's stop_pts=150 could fire (the move never
traded through intermediate levels -- it gapped), and the position only got
closed ~90min later by check_max_hold_safety_net, by which point the P&L
was already locked in.

Compares current LIVE recipe (unmodified) vs LIVE + pre_open_flatten_hhmm="08:40"
across the same 22-day corrected full-day (00:00-23:59) window used for the
month-long replay and CELL_TUNE_V2 comparison reports (2026-08-09).

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

FLATTEN_HHMM = "08:40"


def run_one_day(day: str) -> dict | None:
    patch_nq_gate_for_backfill(lookback_days=60)

    base_recipe = deepcopy(PAPER_RECIPE)
    base_recipe.setdefault("hang_anchor", "O")

    source = SOURCE_FOR_DAY[day]
    rows = load_day(day, source=source)
    if not rows:
        return None
    r_base = replay_day(day, rows, base_recipe)
    r_flat = replay_day(day, rows, base_recipe, pre_open_flatten_hhmm=FLATTEN_HHMM)
    diff = r_flat["sum_pnl"] - r_base["sum_pnl"]
    return dict(
        day=day, base_n=r_base["n_trades"], base_pnl=r_base["sum_pnl"],
        flat_n=r_flat["n_trades"], flat_pnl=r_flat["sum_pnl"],
        n_pre_open_flatten=r_flat["n_pre_open_flatten"], diff=round(diff, 1),
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

    base_vals = [r["base_pnl"] for r in rows_out]
    flat_vals = [r["flat_pnl"] for r in rows_out]
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
        base=stats(base_vals),
        pre_open_flatten=stats(flat_vals),
        diff_flatten_minus_base=dict(
            mean=round(mean_diff, 2), std=round(std_diff, 2),
            t=round(t_stat, 3), p=round(p_val, 4) if p_val is not None else None,
        ),
        n_days_flatten_triggered=sum(1 for r in rows_out if r["n_pre_open_flatten"] > 0),
    )
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    out_path = "reports/research/channel_lab/tmf_replay_pre_open_flatten_test_result.json"
    with open(out_path, "w") as f:
        json.dump(dict(rows=rows_out, summary=summary), f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
