#!/usr/bin/env python3
"""Phase 3 · C18acc × 1 分 K Extension radar sweep · 專業報告。

用法：
  PYTHONPATH=src python scripts/run_c18acc_phase3_1m_sweep.py
  PYTHONPATH=src python scripts/run_c18acc_phase3_1m_sweep.py \\
    --date-start 2024-01-01 --date-end 2026-06-26
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.c18acc_intraday_1m_hold import (  # noqa: E402
    render_c18acc_phase3_report_md,
    run_phase3_1m_sweep,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc Phase 3 · 1m extension sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_phase3_1m_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            oos_start=args.oos_start,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_phase3_1m_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or RESEARCH_RRG / f"{stamp}_c18acc_phase3_1m_report.md"
    md_path.write_text(render_c18acc_phase3_report_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    adopted = payload.get("adopted_candidate") or {}
    print(
        f"Adopted candidate: {adopted.get('variant_id')} "
        f"delta={adopted.get('delta_vs_i0_pp')}pp excess={adopted.get('mean_excess_pct')}%"
    )
    for hid, r in (payload.get("hypothesis_results") or {}).items():
        print(f"  {hid}: {'PASS' if r.get('pass') else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
