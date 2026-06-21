#!/usr/bin/env python3
"""Validate Zweig/Deemer breadth impulse vs Breadth zone-only · param sweep."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.breadth_impulse_validation import (  # noqa: E402
    persist_validation_artifacts,
    render_validation_markdown,
    run_breadth_impulse_validation,
    sweep_breadth_impulse_params,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from report_paths import REPORTS_RESEARCH  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Breadth impulse validation · Zweig + Deemer")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--start", default="2020-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--sweep", action="store_true", help="Grid-search params · pick best")
    p.add_argument("--report", type=Path, default=None)
    args = p.parse_args()

    stamp = date.today().strftime("%Y%m%d")
    report = args.report or (REPORTS_RESEARCH / f"{stamp}_breadth_impulse_validation.md")

    conn = connect(args.db)
    try:
        if args.sweep:
            best_params, result, sweep_df = sweep_breadth_impulse_params(
                conn, start_date=args.start, end_date=args.end
            )
            note = " · **sweep best**"
        else:
            result = run_breadth_impulse_validation(
                conn, start_date=args.start, end_date=args.end
            )
            best_params = result.params
            sweep_df = None
            note = ""
    finally:
        conn.close()

    end_date = str(result.summary.iloc[0].get("end_date", args.start))
    if "end_date" not in result.summary.columns:
        # infer from validation run
        end_date = args.end or "latest"

    md = render_validation_markdown(
        result,
        sweep_df,
        start_date=args.start,
        end_date=end_date if end_date != "latest" else "latest DB",
        params_note=note,
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(md, encoding="utf-8")
    json_path = persist_validation_artifacts(result, sweep_df, report_path=report)
    print(f"Wrote {report}")
    print(f"Wrote {json_path}")

    print("\n=== Overlay A/B ===")
    print(
        result.summary[
            ["variant", "total_return_pct", "excess_return_pct", "sharpe", "max_drawdown_pct"]
        ].to_string(index=False)
    )
    print("\n=== Incremental (LuxAlgo − zone) ===")
    for k, v in result.incremental.items():
        print(f"  {k}: {v}")
    if args.sweep:
        print(f"\nBest params: {best_params}")
        print(sweep_df.head(3).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
