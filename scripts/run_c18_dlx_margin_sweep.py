#!/usr/bin/env python3
"""C18-dlx · 買前3日/持後3日 各自 ā · margin 0.06–0.10。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_score_swap_c import C18_DLX_MARGIN_SWEEP, run_score_swap_c_sweep
from report_paths import RESEARCH_RRG
from stock_db import DEFAULT_DB_PATH, connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18-dlx split entry avg accel sweep")
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
            configs=C18_DLX_MARGIN_SWEEP,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_c18_dlx_margin_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    refs = {s["variant_id"]: s for s in payload["summaries"]}
    print(f"C18={refs['C18']['mean_excess_pct']}% · dls1={refs['C18-dls1']['mean_excess_pct']}%")
    for vid in [f"C18-dlx{m}" for m in (6, 7, 8, 9, 10)] + ["C18-dlx8b"]:
        s = refs[vid]
        print(
            f"  {vid:12} margin={s.get('effective_margin')} excess={s.get('mean_excess_pct')}% "
            f"swaps={s.get('swaps_total')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
