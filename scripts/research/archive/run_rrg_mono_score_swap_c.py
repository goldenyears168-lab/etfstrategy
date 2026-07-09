#!/usr/bin/env python3
"""RRG mono · 模式 C：純 seg_last 分數換倉 sweep。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_score_swap_c import run_score_swap_c_sweep  # noqa: E402
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG mono score swap mode C")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2026-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_score_swap_c_sweep(conn, date_start=args.date_start, date_end=args.date_end)
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_rrg_mono_score_swap_c.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    ref_a = payload.get("reference_a_hold7") or {}
    ref_c0 = payload.get("reference_c0_hold7") or {}
    print(f"Ref A hold7={ref_a.get('mean_excess_pct')}% · C0 hold7={ref_c0.get('mean_excess_pct')}%")
    best = payload.get("best") or {}
    print(f"Best {best.get('variant_id')}: mean_excess={best.get('mean_excess_pct')}% swaps={best.get('swaps_total')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
