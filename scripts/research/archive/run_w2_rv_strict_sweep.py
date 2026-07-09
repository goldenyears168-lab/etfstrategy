#!/usr/bin/env python3
"""WMA2 strict dip sweep · structural W20+W5 high · W2 RV threshold grid.

  PYTHONPATH=src python3 scripts/run_w2_rv_strict_sweep.py
  PYTHONPATH=src python3 scripts/run_w2_rv_strict_sweep.py --focus-threshold 99.3
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
from research.backtest.triple_wma_pullback_sweep import (  # noqa: E402
    DEFAULT_W2_RV_THRESHOLDS,
    render_w2_rv_strict_md,
    run_w2_rv_strict_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WMA2 RV strict threshold sweep")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--intraday-tail-days", type=int, default=60)
    ap.add_argument("--focus-threshold", type=float, default=99.3)
    ap.add_argument(
        "--thresholds",
        default=",".join(str(t) for t in DEFAULT_W2_RV_THRESHOLDS),
        help="Comma-separated W2 RV max values",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    rv_thresholds = tuple(float(x.strip()) for x in args.thresholds.split(",") if x.strip())

    conn = connect(args.db)
    try:
        payload = run_w2_rv_strict_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            intraday_tail_days=args.intraday_tail_days,
            rv_thresholds=rv_thresholds,
            focus_threshold=args.focus_threshold,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_w2_rv_strict_sweep.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_w2_rv_strict_md(payload), encoding="utf-8")

    verdict = payload.get("verdict") or {}
    print(verdict.get("summary", ""))
    for r in verdict.get("reasons") or []:
        print(f"  · {r}")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
