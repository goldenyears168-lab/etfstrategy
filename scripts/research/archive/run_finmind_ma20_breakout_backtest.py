#!/usr/bin/env python3
"""FinMind marketplace · 突破月線長紅放量 · backtest runner.

Phase 0:
  PYTHONPATH=src .venv/bin/python scripts/run_finmind_ma20_breakout_backtest.py --validate-only

Baseline (hold 10d):
  PYTHONPATH=src .venv/bin/python scripts/run_finmind_ma20_breakout_backtest.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.finmind_ma20_breakout_backtest import (  # noqa: E402
    render_ma20_breakout_markdown,
    run_ma20_breakout_backtest,
)
from research.backtest.finmind_ma20_breakout_common import (  # noqa: E402
    DEFAULT_DATE_START,
    validate_ma20_breakout_data,
)
from research.backtest.slot_backtest_summary import write_slot_backtest_summary  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FinMind MA20 breakout volume backtest")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--universe", default="tw100", choices=("tw100", "etf_watchlist", "both"))
    parser.add_argument("--date-start", default=DEFAULT_DATE_START)
    parser.add_argument("--date-end", default=None)
    parser.add_argument("--hold-days", type=int, default=10)
    parser.add_argument("--n-slots", type=int, default=5)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "reports" / "research" / "finmind-marketplace",
    )
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)

    if args.validate_only:
        report = validate_ma20_breakout_data(
            args.db,
            universe=args.universe,
            date_start=args.date_start,
            date_end=args.date_end,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("phase0_pass") else 1

    result = run_ma20_breakout_backtest(
        db_path=args.db,
        universe=args.universe,
        date_start=args.date_start,
        date_end=args.date_end,
        n_slots=args.n_slots,
        hold_days=args.hold_days,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = args.out_dir / f"ma20_breakout_volume_backtest_{stamp}.json"
    md_path = args.out_dir / f"ma20_breakout_volume_backtest_{stamp}.md"

    write_slot_backtest_summary(json_path, result)
    md_path.write_text(render_ma20_breakout_markdown(result), encoding="utf-8")

    print(render_ma20_breakout_markdown(result))
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
