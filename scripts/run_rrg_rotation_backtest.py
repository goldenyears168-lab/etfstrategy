#!/usr/bin/env python3
"""RRG 四象限月頻回測（de Kempenaer · TradingView WMA）。"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rrg_rotation import render_rrg_backtest_markdown, run_rrg_monthly_backtest  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="RRG quadrant monthly backtest vs IX0001")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--start-month", default="2023-01")
    p.add_argument("--end-month", default=date.today().strftime("%Y-%m"))
    p.add_argument("--length", type=int, default=20, help="WMA window (TradingView default 20)")
    p.add_argument("--hold-days", type=int, default=9)
    p.add_argument("--min-vcp", type=float, default=None, help="VCP composite floor (default from config)")
    p.add_argument("--skip-vcp", action="store_true", help="Skip historical VCP scoring (faster)")
    p.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Markdown report path (default reports/YYYYMMDD_rrg_rotation_backtest.md)",
    )
    args = p.parse_args()

    report = args.report or (
        ROOT / "reports" / f"{date.today().strftime('%Y%m%d')}_rrg_rotation_backtest.md"
    )

    conn = connect(args.db)
    try:
        result = run_rrg_monthly_backtest(
            conn,
            start_month=args.start_month,
            end_month=args.end_month,
            length=args.length,
            hold_days=args.hold_days,
            min_vcp_score=args.min_vcp,
            include_vcp=not args.skip_vcp,
        )
    finally:
        conn.close()

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_rrg_backtest_markdown(result), encoding="utf-8")
    print(f"Wrote {report}")

    print("\n=== Cohort summary ===")
    for row in result["cohort_summary"].values():
        print(
            f"{row['label']}: n={row.get('n_periods')} "
            f"win={row.get('win_rate_vs_bench_pct')}% "
            f"excess={row.get('mean_excess_pct')}"
        )
    print(f"\nQuadrant flip rate (252d): {result['quadrant_flip'].get('mean_flip_rate_pct')}%")
    best = result.get("best_breadth_zone_200")
    if best:
        print(
            f"\nBest 200MA breadth zone: {best['display']} "
            f"(excess={best['mean_excess_pct']}%, n={best['n_periods']})"
        )
    live = result.get("live_vcp_crossval") or []
    if live:
        n_lead = sum(1 for r in live if r.get("rrg_leading"))
        print(f"Live VCP×RRG overlap Leading: {n_lead}/{len(live)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
