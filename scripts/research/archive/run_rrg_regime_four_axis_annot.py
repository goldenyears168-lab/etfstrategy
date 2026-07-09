#!/usr/bin/env python3
"""RRG × Regime four-axis · Phase 0 annotation (no rule change).

用法：
  PYTHONPATH=src python scripts/run_rrg_regime_four_axis_annot.py
  PYTHONPATH=src python scripts/run_rrg_regime_four_axis_annot.py \\
    --date-start 2018-01-02 --date-end auto
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
from research.backtest.archive.rrg_regime_four_axis_annot import (  # noqa: E402
    render_rrg_regime_four_axis_annot_md,
    run_rrg_regime_four_axis_annot,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _db_max_date(conn) -> str:
    from market_benchmark import load_benchmark_close

    bench = load_benchmark_close(conn)
    return str(bench.index.max())[:10]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG × Regime four-axis Phase 0 annot")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default=None, help="default = lifecycle meta start")
    parser.add_argument("--date-end", default="auto", help="auto = DB latest signal day")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        date_end = None if args.date_end == "auto" else args.date_end
        if args.date_end == "auto":
            date_end = _db_max_date(conn)
        payload = run_rrg_regime_four_axis_annot(
            conn,
            date_start=args.date_start,
            date_end=date_end,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_rrg_regime_four_axis_annot.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_rrg_regime_four_axis_annot_md(payload), encoding="utf-8")

    h_ra2 = payload["hypotheses"]["H-RA-2"]["compare"]
    h_s3 = payload["hypotheses"]["H-S3-BENCH"]["compare"]
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(f"  events={payload['n_events']} range={payload['date_start']}→{payload['date_end']}")
    print(
        f"  H-RA-2 S2 bench≥0: flagged n={h_ra2['flagged_n']} Δ={h_ra2['delta_pp']}pp pass={h_ra2['pass']}"
    )
    print(
        f"  H-S3-BENCH: bench_nxt≥0 Δ={h_s3['delta_pp']}pp "
        f"({h_s3['flagged_mean']} vs {h_s3['unflagged_mean']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
