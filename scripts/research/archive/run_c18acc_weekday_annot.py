#!/usr/bin/env python3
"""C18acc+S2 · weekday execution Phase 0 annotation (no rule change).

用法：
  PYTHONPATH=src python scripts/run_c18acc_weekday_annot.py
  PYTHONPATH=src python scripts/run_c18acc_weekday_annot.py \\
    --date-start 2020-01-02 --date-end 2026-07-03 --profile timeline
  PYTHONPATH=src python scripts/run_c18acc_weekday_annot.py --profile pool1
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
from research.backtest.archive.c18acc_weekday_annot import (  # noqa: E402
    render_c18acc_weekday_annot_md,
    run_c18acc_weekday_annot,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _db_max_date(conn) -> str:
    from market_benchmark import load_benchmark_close

    bench = load_benchmark_close(conn)
    return str(bench.index.max())[:10]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc+S2 · weekday Phase 0 annot")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2020-01-02")
    parser.add_argument("--date-end", default="auto", help="auto = DB latest")
    parser.add_argument("--n-slots", type=int, default=3)
    parser.add_argument(
        "--profile",
        choices=("timeline", "pool1"),
        default="timeline",
        help="timeline=fresh+close (HTML baseline); pool1=fresh∪accel poll_5m",
    )
    parser.add_argument(
        "--intraday",
        action="store_true",
        help="timeline profile only: use poll_5m instead of close",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        date_end = _db_max_date(conn) if args.date_end == "auto" else args.date_end
        payload = run_c18acc_weekday_annot(
            conn,
            date_start=args.date_start,
            date_end=date_end,
            n_slots=args.n_slots,
            profile=args.profile,
            use_close_timing=not args.intraday,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    suffix = f"_{args.profile}"
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_weekday_annot{suffix}.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_weekday_annot_md(payload), encoding="utf-8")

    h0 = payload["splits"]["FULL"]["h_wd_0_entry"]
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(
        f"  legs={payload['n_legs']} profile={payload['profile']} "
        f"mean_excess={payload['summary'].get('mean_excess_pct')}%"
    )
    print(
        f"  H-WD-0: Fri n={h0['friday_n']} excess={h0['friday_mean_excess_pct']} · "
        f"non-Fri n={h0['non_friday_n']} excess={h0['non_friday_mean_excess_pct']} · "
        f"Δ={h0['delta_excess_pp']}pp pass={h0['h_wd_0_pass']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
