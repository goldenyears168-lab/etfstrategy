#!/usr/bin/env python3
"""FinMind marketplace · ensemble team backtest.

  PYTHONPATH=src .venv/bin/python scripts/run_finmind_marketplace_ensemble.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.finmind_marketplace_ensemble import run_ensemble_analysis  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402


def main() -> int:
    result = run_ensemble_analysis(
        db_path=DEFAULT_DB_PATH,
        out_dir=ROOT / "reports" / "research" / "finmind-marketplace",
    )
    for rep in result["windows"]:
        w = rep["window"]
        print(f"=== {w['date_start']} ~ {w['date_end']} ===")
        for row in rep["rows"]:
            beat = "WIN" if row.get("beats_bench") else "lose"
            print(
                f"  {row['label']} [{row['mode']}]: "
                f"total={row.get('total_return_pct')}% excess={row.get('interval_excess_pct')}% ({beat})"
            )
        print()
    print(f"Wrote {result['json_path']}")
    print(f"Wrote {result['md_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
