#!/usr/bin/env python3
"""C18acc snapshot_1300 sweep · 13:00 full_rrg 訊號 + 13:00 成交。

用法：
  PYTHONPATH=src python3 scripts/run_c18acc_snapshot_1300_sweep.py
  PYTHONPATH=src python3 scripts/run_c18acc_snapshot_1300_sweep.py \\
    --date-start 2021-01-04 --date-end auto
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
from research.backtest.c18acc_snapshot_1300 import (  # noqa: E402
    SAMPLE_START_DEFAULT,
    render_c18acc_snapshot_1300_sweep_md,
    run_c18acc_snapshot_1300_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _db_max_date(conn) -> str:
    from market_benchmark import load_benchmark_close

    bench = load_benchmark_close(conn)
    return str(bench.index.max())[:10]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc snapshot_1300 sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default=SAMPLE_START_DEFAULT)
    parser.add_argument("--date-end", default="auto")
    parser.add_argument("--is-end", default="2025-12-31")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--n-slots", type=int, default=3)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        date_end = _db_max_date(conn) if args.date_end == "auto" else args.date_end
        payload = run_c18acc_snapshot_1300_sweep(
            conn,
            date_start=args.date_start,
            date_end=date_end,
            is_end=args.is_end,
            oos_start=args.oos_start,
            n_slots=args.n_slots,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_snapshot_1300_sweep.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_snapshot_1300_sweep_md(payload), encoding="utf-8")

    full = (payload.get("windows") or {}).get("FULL") or {}
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    for vid in ("S0", "S1", "S2", "S2b"):
        row = full.get(vid) or {}
        print(
            f"  {vid} excess={row.get('mean_excess_pct')}% swaps={row.get('swaps_total')} "
            f"skip={row.get('snapshot_days_skipped')}"
        )
    cov = payload.get("kbar_coverage") or {}
    print(f"  kbar coverage={cov.get('coverage_pct')}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
