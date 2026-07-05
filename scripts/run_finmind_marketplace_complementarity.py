#!/usr/bin/env python3
"""FinMind marketplace · daily complementarity / team-building analysis.

  PYTHONPATH=src .venv/bin/python scripts/run_finmind_marketplace_complementarity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.finmind_marketplace_complementarity import (  # noqa: E402
    run_complementarity_analysis,
)
from stock_db import DEFAULT_DB_PATH  # noqa: E402


def main() -> int:
    result = run_complementarity_analysis(
        db_path=DEFAULT_DB_PATH,
        out_dir=ROOT / "reports" / "research" / "finmind-marketplace",
    )
    data = result["data"]
    print("=== Daily rhythm ===")
    for r in data["rhythm"]:
        print(
            f"  {r['short_id']}: signal_days={r['signal_days']} "
            f"({r['pct_calendar_signal']}%) exposure={r['exposure_pct']}%"
        )
    print()
    print("=== Top complements ===")
    for p in sorted(data["pairwise"], key=lambda x: x["complement_score"], reverse=True)[:5]:
        print(
            f"  {p['a']}×{p['b']}: complement={p['complement_score']} "
            f"jaccard={p['signal_day_jaccard']} pattern={p['pattern']}"
        )
    print()
    print(f"Wrote {result['json_path']}")
    print(f"Wrote {result['md_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
