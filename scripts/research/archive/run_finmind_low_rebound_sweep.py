#!/usr/bin/env python3
"""FinMind #1 · Phase 2 sensitivity sweep (hold / SL-TP).

  PYTHONPATH=src .venv/bin/python scripts/run_finmind_low_rebound_sweep.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.finmind_low_rebound_backtest import (  # noqa: E402
    PHASE2_EXIT_GRID,
    LowReboundExitRule,
    render_sweep_markdown,
    run_low_rebound_sweep_cell,
)
from stock_db import DEFAULT_DB_PATH  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FinMind low-rebound Phase 2 sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--universe", default="tw100", choices=("tw100", "etf_watchlist", "both"))
    parser.add_argument("--date-start", default="2015-01-01")
    parser.add_argument("--date-end", default=None)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "reports" / "research" / "finmind-marketplace",
    )
    args = parser.parse_args(argv)

    cells: list[dict] = []
    for rule in PHASE2_EXIT_GRID:
        print(f"Running {rule.variant_id} ...", flush=True)
        cells.append(
            run_low_rebound_sweep_cell(
                db_path=args.db,
                universe=args.universe,
                date_start=args.date_start,
                date_end=args.date_end,
                exit_rule=rule,
            )
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = args.out_dir / f"low_rebound_foreign_it_sync_sweep_{stamp}.json"
    md_path = args.out_dir / f"low_rebound_foreign_it_sync_sweep_{stamp}.md"
    payload = {
        "phase": "phase2_sensitivity",
        "universe": args.universe,
        "date_start": args.date_start,
        "date_end": args.date_end or date.today().isoformat(),
        "grid": [r.variant_id for r in PHASE2_EXIT_GRID],
        "cells": cells,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_sweep_markdown(cells), encoding="utf-8")

    print(render_sweep_markdown(cells))
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
