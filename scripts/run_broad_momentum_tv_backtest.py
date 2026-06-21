#!/usr/bin/env python3
"""Backtest & compare broad-momentum TV strategies (Antonacci · Minervini SEPA · ADX)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.broad_momentum_tv_backtest import (  # noqa: E402
    persist_saved_strategy_artifacts,
    render_backtest_markdown,
    run_all_broad_momentum_backtests,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from report_paths import RESEARCH_BREADTH, REPORTS_RESEARCH  # noqa: E402

REPORTS = REPORTS_RESEARCH


def main() -> int:
    p = argparse.ArgumentParser(description="Broad-momentum TV strategy comparison backtest")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--start", default="2024-01-01", help="Backtest start (YYYY-MM-DD)")
    p.add_argument("--end", default=None, help="Backtest end (default: latest in DB)")
    p.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Markdown report path",
    )
    args = p.parse_args()

    stamp = date.today().strftime("%Y%m%d")
    report = args.report or (REPORTS / f"{stamp}_broad_momentum_tv_backtest.md")

    conn = connect(args.db)
    try:
        summary, results, regime_slice = run_all_broad_momentum_backtests(
            conn,
            start_date=args.start,
            end_date=args.end,
        )
    finally:
        conn.close()

    end_date = results[0].end_date if results else args.start
    md = render_backtest_markdown(
        summary,
        results,
        regime_slice,
        start_date=args.start,
        end_date=end_date,
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(md, encoding="utf-8")
    print(f"Wrote {report}")

    saved_paths = persist_saved_strategy_artifacts(results)
    for sid, path in saved_paths.items():
        print(f"Saved {sid} → {path}")

    print("=== Performance summary ===")
    cols = ["strategy", "total_return_pct", "excess_return_pct", "sharpe", "max_drawdown_pct"]
    print(summary[cols].to_string(index=False))

    if regime_slice is not None and not regime_slice.empty:
        print("\n=== broad_momentum slice ===")
        print(regime_slice.to_string(index=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
