#!/usr/bin/env python3
"""Dual-WMA fast-chart sweep · WMA(3) vs WMA(5) with WMA(20) anchor.

  PYTHONPATH=src python3 scripts/run_dual_wma_short_length_sweep.py
  PYTHONPATH=src python3 scripts/run_dual_wma_short_length_sweep.py --no-intraday
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.dual_wma_short_length_sweep import (  # noqa: E402
    render_dual_wma_short_length_md,
    run_dual_wma_short_length_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WMA(3) vs WMA(5) fast-chart sweep")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument(
        "--intraday-tail-days",
        type=int,
        default=40,
        help="Last N trade dates for intraday signal sample (0=skip)",
    )
    ap.add_argument("--no-intraday", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    tail = 0 if args.no_intraday else args.intraday_tail_days
    conn = connect(args.db)
    try:
        payload = run_dual_wma_short_length_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            intraday_tail_days=tail,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_dual_wma_short_length_sweep.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_dual_wma_short_length_md(payload), encoding="utf-8")

    verdict = payload.get("verdict") or {}
    print(verdict.get("summary", ""))
    for r in verdict.get("reasons") or []:
        print(f"  · {r}")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
