#!/usr/bin/env python3
"""I36 fullstack · extension entry + combo_spike exit on C18acc champion.

用法：
  PYTHONPATH=src python3 scripts/run_c18acc_i36_fullstack_sweep.py
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
    render_i36_fullstack_report_md,
    run_i36_fullstack_analysis,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc I36 fullstack entry+exit sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--n-boot", type=int, default=10000)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_i36_fullstack_analysis(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            oos_start=args.oos_start,
            n_boot=args.n_boot,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_i36_fullstack_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or RESEARCH_RRG / f"{stamp}_c18acc_i36_fullstack_report.md"
    md_path.write_text(render_i36_fullstack_report_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    rec = payload.get("recommended") or {}
    print(f"Best: {rec.get('best_id')} · action={rec.get('action')}")
    for hid, r in (payload.get("hypothesis_results") or {}).items():
        print(f"  {hid}: {'PASS' if r.get('pass') else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
