#!/usr/bin/env python3
"""Cross-strategy ensemble · native + FinMind · config/backtest_standard.yaml SSOT.

  PYTHONPATH=src .venv/bin/python scripts/run_cross_strategy_ensemble.py
  PYTHONPATH=src .venv/bin/python scripts/run_cross_strategy_ensemble.py --window cmp_max_common
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.cross_strategy_ensemble import run_cross_strategy_ensemble  # noqa: E402
from research.backtest.unified_comparison import run_unified_comparison  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cross-strategy ensemble (config SSOT)")
    parser.add_argument(
        "--window",
        default="cmp_max_common",
        help="window_id filter for ensemble teams",
    )
    parser.add_argument(
        "--with-singles",
        action="store_true",
        help="also refresh single-track league table",
    )
    args = parser.parse_args(argv)

    if args.with_singles:
        print("Refreshing single-track league table…")
        singles = run_unified_comparison(window_id=args.window)
        print(f"  Wrote {singles['md_path']}")

    print(f"Running cross-strategy ensembles · window={args.window}")
    result = run_cross_strategy_ensemble(window_id=args.window)
    for row in result["payload"]["rows"]:
        beat = "WIN" if row.get("beats_bench") else "lose"
        print(
            f"  #{row['rank']} {row['label']}: "
            f"excess={row.get('interval_excess_pct')}% ({beat})"
        )
    print(f"Wrote {result['json_path']}")
    print(f"Wrote {result['md_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
