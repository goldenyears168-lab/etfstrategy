#!/usr/bin/env python3
"""C18acc W6 parameter micro-tune · daily RRG weakening stop + I36 stack。

用法：
  PYTHONPATH=src python scripts/run_c18acc_w6_tune_sweep.py
  PYTHONPATH=src python scripts/run_c18acc_w6_tune_sweep.py \\
    --date-start 2024-01-01 --date-end 2026-06-30
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.c18acc_w6_tune_sweep import (  # noqa: E402
    render_w6_tune_md,
    run_w6_tune_sweep,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc W6 parameter micro-tune sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-30")
    parser.add_argument("--is-end", default="2025-12-31")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        payload = run_w6_tune_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_start=args.oos_start,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_w6_tune_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out.with_suffix(".md")
    md_path.write_text(render_w6_tune_md(payload), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Wrote {md_path}")

    rec = payload.get("recommendation") or {}
    print(f"Recommendation: {rec.get('action')} — {rec.get('note')}")
    for row in payload.get("top3_full") or []:
        print(
            f"  {row.get('variant_id')}: FULL Δ={row.get('full_delta_i36_pp')} "
            f"IS={row.get('is_delta_i36_pp')} OOS={row.get('oos_delta_i36_pp')} "
            f"A2 full={'Y' if row.get('full_a2') else 'N'} oos={'Y' if row.get('oos_a2') else 'N'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
