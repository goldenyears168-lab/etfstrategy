#!/usr/bin/env python3
"""FinMind strategy marketplace · unified cross-strategy comparison.

  PYTHONPATH=src .venv/bin/python scripts/run_finmind_marketplace_comparison.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.finmind_marketplace_comparison import run_marketplace_comparison  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402


def main() -> int:
    result = run_marketplace_comparison(
        db_path=DEFAULT_DB_PATH,
        out_dir=ROOT / "reports" / "research" / "finmind-marketplace",
    )
    print(result["windows"][0]["comparison_contract"])
    print()
    for table in result["windows"]:
        print(f"=== {table['window_id']} ({table['date_start']} ~ {table['date_end']}) ===")
        for row in table["rows"]:
            print(
                f"  #{row['rank']} {row['label']}: "
                f"interval_excess={row.get('interval_excess_pct')}% · "
                f"total={row.get('total_return_pct')}% · n={row.get('n_periods')}"
            )
        print()
    print(f"Wrote {result['json_path']}")
    print(f"Wrote {result['md_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
