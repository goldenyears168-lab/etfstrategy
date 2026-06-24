#!/usr/bin/env python3
"""Phase 1 · 買方 recent_accel_up gate · acel3-5 / dls1 對照。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_score_swap_c import C18_BUY_ACCEL_PHASE1_SWEEP, run_score_swap_c_sweep
from report_paths import RESEARCH_RRG
from stock_db import DEFAULT_DB_PATH, connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18 buy-side accel Phase 1 sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_score_swap_c_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            configs=C18_BUY_ACCEL_PHASE1_SWEEP,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18_buy_accel_phase1_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    for s in sorted(payload["summaries"], key=lambda x: -(x.get("mean_excess_pct") or -999)):
        gate = s.get("challenger_gate") or "none"
        print(
            f"  {s['variant_id']:20} chall={gate:16} margin={s.get('score_margin')} "
            f"excess={s.get('mean_excess_pct')}% swaps={s.get('swaps_total')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
