#!/usr/bin/env python3
"""List C18acc fresh+mono picks at 13:20 vs 13:30 for recent months.

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/run_c18acc_1320_vs_1330_picks.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_c18acc_1320_vs_1330_picks.py --months 3
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.c18acc_1320_vs_1330_picks import (  # noqa: E402
    render_1320_vs_1330_md,
    run_1320_vs_1330_picks,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="13:20 vs 13:30 fresh picks")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--date-start", default=None)
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_1320_vs_1330_picks(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            months=args.months,
            top_n=args.top_n,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_1320_vs_1330_picks.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_1320_vs_1330_md(payload), encoding="utf-8")
    s = payload.get("summary") or {}
    print(
        f"ok={s.get('n_ok')} set_diff={s.get('n_days_set_diff')} "
        f"top3_diff={s.get('n_days_top3_diff')} ({s.get('pct_days_top3_diff')}%)"
    )
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
