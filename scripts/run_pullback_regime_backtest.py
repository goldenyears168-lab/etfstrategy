#!/usr/bin/env python3
"""Pullback TV rules × Momentum Correction regime backtest."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.pullback_regime_backtest import (  # noqa: E402
    PullbackRegimeBacktestConfig,
    render_pullback_regime_markdown,
    run_pullback_regime_backtest,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Pullback rules backtest on momentum_correction days")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--date-start", default="2024-01-01")
    p.add_argument("--date-end", default="2026-12-31")
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--min-vol", type=int, default=3_000_000)
    p.add_argument("--hold-days", type=int, nargs="+", default=[30, 60])
    p.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Markdown report (default reports/YYYYMMDD_pullback_correction_backtest.md)",
    )
    p.add_argument("--json", type=Path, default=None, help="Optional JSON dump")
    args = p.parse_args()

    report = args.report or (
        ROOT / "reports" / f"{date.today().strftime('%Y%m%d')}_pullback_correction_backtest.md"
    )
    cfg = PullbackRegimeBacktestConfig(
        date_start=args.date_start,
        date_end=args.date_end,
        top_n=args.top_n,
        min_vol=args.min_vol,
        horizons=tuple(args.hold_days),
    )

    conn = connect(args.db)
    try:
        result = run_pullback_regime_backtest(conn, cfg)
    finally:
        conn.close()

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_pullback_regime_markdown(result), encoding="utf-8")
    print(f"Wrote {report}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")

    for s in sorted(result["summaries"], key=lambda x: (-(x["mean_excess_pct"] or -999), x["hold_days"])):
        me = s["mean_excess_pct"]
        wr = s["win_rate_vs_bench_pct"]
        print(
            f"H{s['hold_days']:>2} {s['strategy_id']:<22} n={s['n_periods']:>3} "
            f"win={wr}% mean_excess={me}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
