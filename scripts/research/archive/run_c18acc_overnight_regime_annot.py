#!/usr/bin/env python3
"""C18acc · overnight regime gate · Phase 0 annotation (no rule change).

用法：
  PYTHONPATH=src python scripts/run_c18acc_overnight_regime_annot.py
  PYTHONPATH=src python scripts/run_c18acc_overnight_regime_annot.py \\
    --date-start 2020-01-02 --date-end auto
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
from research.backtest.archive.c18acc_overnight_regime_annot import (  # noqa: E402
    render_c18acc_overnight_regime_annot_md,
    run_c18acc_overnight_regime_annot,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _db_max_date(conn) -> str:
    from market_benchmark import load_benchmark_close

    bench = load_benchmark_close(conn)
    return str(bench.index.max())[:10]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc · overnight regime Phase 0 annot")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2020-01-02")
    parser.add_argument("--date-end", default="auto", help="auto = DB latest")
    parser.add_argument("--n-slots", type=int, default=3)
    parser.add_argument("--case-date", default="2026-07-07")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        date_end = _db_max_date(conn) if args.date_end == "auto" else args.date_end
        payload = run_c18acc_overnight_regime_annot(
            conn,
            date_start=args.date_start,
            date_end=date_end,
            n_slots=args.n_slots,
            case_date=args.case_date,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_overnight_regime_annot.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_overnight_regime_annot_md(payload), encoding="utf-8")

    h0 = payload["splits"]["FULL"]["h_or_0"]
    case = payload.get("case_study") or {}
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(
        f"  legs={payload['n_legs']} mean_excess={payload['summary'].get('mean_excess_pct')}% "
        f"sox_from={payload.get('effective_start_sox')}"
    )
    print(
        f"  H-OR-0: flagged n={h0['flagged_n']} mean_ret={h0['flagged_mean_return_pct']} · "
        f"unflagged n={h0['unflagged_n']} mean_ret={h0['unflagged_mean_return_pct']} · "
        f"Δ={h0['delta_return_pp']}pp pass={h0['h_or_0_pass']}"
    )
    print(
        f"  case {case.get('case_date')}: entries={case.get('n_entries_r0')} "
        f"E1_block={case.get('virtual_block_e1_sox')} "
        f"E7_block={case.get('virtual_block_e7_sox_up')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
