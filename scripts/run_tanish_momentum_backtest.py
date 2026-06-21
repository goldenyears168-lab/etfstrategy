#!/usr/bin/env python3
"""Backtest tanish35 Multi-Factor Momentum × Breadth zone (Strong / Overbought)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.tanish_momentum_backtest import (  # noqa: E402
    persist_tanish_artifacts,
    render_tanish_backtest_markdown,
    run_tanish_breadth_comparison,
)
from report_paths import RESEARCH_BREADTH  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(
        description="tanish35 NewMom × Breadth zone backtest (TW local DB)"
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--start", default="2026-01-01", help="Backtest start (YYYY-MM-DD)")
    p.add_argument("--end", default=None, help="Backtest end (default: latest in DB)")
    p.add_argument(
        "--warmup-start",
        default="2024-01-01",
        help="Indicator warm-up start (needs ≥252d before --start)",
    )
    p.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Markdown report path",
    )
    args = p.parse_args()

    conn = connect(args.db)
    try:
        payload = run_tanish_breadth_comparison(
            conn,
            start_date=args.start,
            end_date=args.end,
            warmup_start=args.warmup_start,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    end_date = payload["end_date"]
    report = args.report or (
        RESEARCH_BREADTH / f"{stamp}_tanish_momentum_breadth_{str(end_date).replace('-', '')}.md"
    )
    md = render_tanish_backtest_markdown(payload)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(md, encoding="utf-8")
    json_path = persist_tanish_artifacts(payload)

    print(f"Wrote {report}")
    print(f"Wrote {json_path}")
    print()
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
