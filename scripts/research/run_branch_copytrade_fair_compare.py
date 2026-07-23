#!/usr/bin/env python3
"""Fair multi-branch Copytrade comparison using PIT net-buy amount.

  PYTHONPATH=src .venv/bin/python scripts/research/run_branch_copytrade_fair_compare.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_branch_copytrade_fair_compare.py \\
      --start 2024-07-01 --end 2026-07-16 --oos-cut 2025-07-01
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.branch_copytrade_fair_compare import (  # noqa: E402
    DEFAULT_BRANCHES,
    BranchSpec,
    run_fair_compare,
    write_artifacts,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Fair branch amount copytrade compare")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--oos-cut", default="2025-07-01")
    ap.add_argument(
        "--branches",
        default=",".join(b.slug for b in DEFAULT_BRANCHES),
        help="Comma slugs from DEFAULT_BRANCHES",
    )
    ap.add_argument("--min-days", type=int, default=60, help="Skip thin tapes")
    args = ap.parse_args()

    wanted = {s.strip() for s in str(args.branches).split(",") if s.strip()}
    specs = tuple(b for b in DEFAULT_BRANCHES if b.slug in wanted)
    if not specs:
        print("ERROR: no matching branches", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        kept: list[BranchSpec] = []
        for b in specs:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT trade_date) AS d
                FROM stock_broker_branch_daily
                WHERE securities_trader_id = ? AND source = 'finmind'
                  AND trade_date >= ? AND trade_date <= ?
                """,
                (b.trader_id, args.start, args.end),
            ).fetchone()
            days = int(row["d"] or 0)
            if days < args.min_days:
                print(
                    f"SKIP {b.label} ({b.trader_id}): only {days} tape days "
                    f"(need ≥{args.min_days})",
                    file=sys.stderr,
                )
                continue
            kept.append(b)
        if not kept:
            print("ERROR: no branches with enough tape", file=sys.stderr)
            return 1

        print(
            f"compare n={len(kept)} window={args.start}..{args.end} "
            f"oos_cut={args.oos_cut}"
        )
        for b in kept:
            print(f"  - {b.label} {b.trader_id}")

        payload = run_fair_compare(
            conn,
            branches=tuple(kept),
            window_start=args.start,
            window_end=args.end,
            oos_cut=args.oos_cut,
        )
        paths = write_artifacts(payload)
        print("wrote", paths["md"])
        print("wrote", paths["json"])
        print("\n=== Density-matched OOS by period return% ===")
        for i, r in enumerate(payload["rank_density_matched_oos"], 1):
            print(
                f"{i}. {r['label']} own={r['min_net_amount_m']:g}M "
                f"oos_n={r['oos_n_cycles']} ret%={r['oos_period_return_pct']} "
                f"mean%/cyc={r.get('oos_mean_return_pct')} "
                f"win%={r['oos_win_rate_pct']}"
            )
        print("\n=== Density-matched Full by period return% ===")
        for i, r in enumerate(payload["rank_density_matched_full"], 1):
            print(
                f"{i}. {r['label']} own={r['min_net_amount_m']:g}M "
                f"full_n={r.get('full_n_cycles')} ret%={r['full_period_return_pct']}"
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
