#!/usr/bin/env python3
"""Export RRG streak cohort panel + lightweight P5 analysis.

Topic: reports/research/rrg-lane-foundation/research_plan.yaml · P5

Examples:
  PYTHONPATH=src .venv/bin/python scripts/export_rrg_streak_cohort_panel.py
  PYTHONPATH=src .venv/bin/python scripts/export_rrg_streak_cohort_panel.py \\
    --out reports/research/rrg-lane-foundation/artifacts/streak_cohort_panel.parquet \\
    --train-end-year 2023 --no-analyze
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_streak_cohort_panel import (  # noqa: E402
    DEFAULT_OUT_DIR,
    analyze_streak_cohort,
    export_streak_cohort_panel,
    write_streak_cohort_analysis,
)

DEFAULT_OUT = DEFAULT_OUT_DIR / "streak_cohort_panel.parquet"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export streak cohort panel (P5)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output parquet/csv path")
    parser.add_argument("--format", choices=("parquet", "csv"), default="parquet")
    parser.add_argument("--year-start", type=int, default=2015)
    parser.add_argument("--year-end", type=int, default=None)
    parser.add_argument("--strong-pct", type=float, default=15.0)
    parser.add_argument("--min-ret-3d-hit", type=float, default=0.0)
    parser.add_argument("--train-end-year", type=int, default=2023)
    parser.add_argument("--miss-list", type=Path, default=None)
    parser.add_argument("--pool-panel", type=Path, default=None)
    parser.add_argument("--no-analyze", action="store_true", help="Skip analysis artifacts")
    args = parser.parse_args(argv)

    meta = export_streak_cohort_panel(
        args.out,
        fmt=args.format,
        year_start=args.year_start,
        year_end=args.year_end,
        strong_pct=args.strong_pct,
        min_ret_3d_hit=args.min_ret_3d_hit,
    )
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"n_rows={meta['n_rows']} hit_rate={meta.get('hit_rate')}")

    if args.no_analyze:
        return 0

    df = pd.read_parquet(args.out)
    analysis = analyze_streak_cohort(
        df,
        train_end_year=args.train_end_year,
        miss_path=args.miss_list,
        pool_panel_path=args.pool_panel,
        strong_pct=args.strong_pct,
    )
    artifacts = write_streak_cohort_analysis(df, analysis=analysis)
    print(json.dumps({"analysis": artifacts, "verdict": analysis.get("verdict")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
