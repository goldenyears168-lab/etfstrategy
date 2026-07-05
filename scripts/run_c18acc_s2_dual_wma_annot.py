#!/usr/bin/env python3
"""C18acc+S2 legs · dual-WMA 回溯註解（Phase W-annot）。

用法：
  PYTHONPATH=src python3 scripts/run_c18acc_s2_dual_wma_annot.py
  PYTHONPATH=src python3 scripts/run_c18acc_s2_dual_wma_annot.py \\
    --date-start 2025-01-02 --date-end 2026-06-30 --n-slots 3
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
from research.backtest.c18acc_s2_dual_wma_annot import (  # noqa: E402
    render_c18acc_s2_dual_wma_annot_md,
    run_c18acc_s2_dual_wma_annot,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc+S2 · dual-WMA leg annotation (W-annot)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2025-01-02")
    parser.add_argument("--date-end", default="2026-06-30")
    parser.add_argument("--n-slots", type=int, default=3)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_c18acc_s2_dual_wma_annot(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            n_slots=args.n_slots,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_s2_dual_wma_annot.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_s2_dual_wma_annot_md(payload), encoding="utf-8")

    c = payload["cohort"]
    among = c["among_s2_force"]
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(
        f"  legs={payload['n_legs']} s2_force={c['n_s2_force_exit']} "
        f"w5_rotation={c['n_w5_rotation_signal']} both={c['n_both']}"
    )
    print(
        f"  among S2: w5_earlier={among['w5_earlier']} same_day={among['same_day']} "
        f"s2_earlier={among['s2_earlier']} median_lead={among['median_lead_days_when_w5_earlier']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
