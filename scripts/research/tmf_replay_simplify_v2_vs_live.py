#!/usr/bin/env python3
"""Order-layer-aware replay comparison: current live recipe (v1.3.0 book,
SPECIALIZED_PATCHES + CELL_TUNE_V2_PATCHES) vs the v1.2.0 baseline
(SPECIALIZED_PATCHES only), across the same corrected full-day (00:00-23:59)
22-day window used for the month-long replay report (2026-08-09).

Reuses replay_day() from tmf_order_layer_aware_replay.py unmodified -- only
the session_pv_book differs between the two recipes fed into it. Both share
the SAME known limitations (1-bar-per-tick granularity, idealized touch
fills, ~60-day NQ gate lookback patch) so the relative comparison should be
fairer than either arm's absolute level.

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
from order.tmf_channel_pv16_book import freeze_cell_book, SPECIALIZED_PATCHES  # noqa: E402
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


def v120_book():
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    return book


def run_one_day(day: str) -> dict | None:
    patch_nq_gate_for_backfill(lookback_days=60)

    live_recipe = deepcopy(PAPER_RECIPE)
    live_recipe.setdefault("hang_anchor", "O")

    simple_recipe = deepcopy(PAPER_RECIPE)
    simple_recipe["session_pv_book"] = v120_book()
    simple_recipe.setdefault("hang_anchor", "O")

    source = SOURCE_FOR_DAY[day]
    rows = load_day(day, source=source)
    if not rows:
        return None
    r_live = replay_day(day, rows, live_recipe)
    r_simple = replay_day(day, rows, simple_recipe)
    diff = r_simple["sum_pnl"] - r_live["sum_pnl"]
    return dict(
        day=day, live_n=r_live["n_trades"], live_pnl=r_live["sum_pnl"],
        simple_n=r_simple["n_trades"], simple_pnl=r_simple["sum_pnl"],
        diff=round(diff, 1),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None, help="run a single day (for parallel batch driving)")
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
    simple_vals = [r["simple_pnl"] for r in rows_out]
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
        simple=stats(simple_vals),
        diff_simple_minus_live=dict(
            mean=round(mean_diff, 2), std=round(std_diff, 2),
            t=round(t_stat, 3), p=round(p_val, 4) if p_val is not None else None,
        ),
    )
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    out_path = "reports/research/channel_lab/tmf_replay_simplify_v2_vs_live_result.json"
    with open(out_path, "w") as f:
        json.dump(dict(rows=rows_out, summary=summary), f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
