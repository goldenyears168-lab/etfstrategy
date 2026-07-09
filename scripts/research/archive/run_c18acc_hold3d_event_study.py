#!/usr/bin/env python3
"""C18acc · hold 3d event-level study vs ABC f1 champion.

  PYTHONPATH=src python3 scripts/research/archive/run_c18acc_hold3d_event_study.py
  PYTHONPATH=src python3 scripts/research/archive/run_c18acc_hold3d_event_study.py --long-window
  PYTHONPATH=src python3 scripts/research/archive/run_c18acc_hold3d_event_study.py \\
    --date-start 2024-01-01 --long-window
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_hold3d_event_study import (  # noqa: E402
    render_c18acc_hold3d_event_study_md,
    run_c18acc_hold3d_event_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc hold 3d event study vs ABC f1")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--intraday-tail-days", type=int, default=90)
    ap.add_argument("--train-days", type=int, default=60)
    ap.add_argument("--val-days", type=int, default=30)
    ap.add_argument(
        "--long-window",
        action="store_true",
        help="Use full date range (no tail trim) · e.g. 2024+",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    t0 = time.perf_counter()
    conn = connect(args.db)
    try:
        payload = run_c18acc_hold3d_event_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            intraday_tail_days=args.intraday_tail_days,
            train_days=args.train_days,
            val_days=args.val_days,
            long_window=args.long_window,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    suffix = "long" if args.long_window else f"{args.intraday_tail_days}d"
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_hold3d_event_{suffix}.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_hold3d_event_study_md(payload), encoding="utf-8")

    v = payload.get("verdict") or {}
    print(v.get("summary", ""))
    for r in v.get("reasons") or []:
        print(f"  · {r}")
    print(f"\nRuntime: {(time.perf_counter() - t0) / 60:.1f} min")
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
