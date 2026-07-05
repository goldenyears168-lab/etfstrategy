#!/usr/bin/env python3
"""C18acc × 1 分 K 延伸出場 sweep · 打破固定 5–10 日持有。

用法：
  PYTHONPATH=src python scripts/run_c18acc_intraday_1m_sweep.py
  PYTHONPATH=src python scripts/run_c18acc_intraday_1m_sweep.py \\
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
    render_intraday_1m_md,
    run_intraday_1m_sweep,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc 1m extension hold sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--round2", action="store_true", help="H-1M-6..10 · I13–I18")
    parser.add_argument("--round3", action="store_true", help="H-1M-11..15 · I19–I30 · hybrid grid")
    parser.add_argument("--round3-fine", action="store_true", help="I31–I40 · I19 參數細掃")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_intraday_1m_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            round2=args.round2,
            round3=args.round3,
            round3_fine=args.round3_fine,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    suffix = (
        "_round3_fine"
        if args.round3_fine
        else ("_round3" if args.round3 else ("_round2" if args.round2 else ""))
    )
    out = args.out or RESEARCH_RRG / f"{stamp}_c18acc_intraday_1m_sweep{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out.with_suffix(".md")
    md_path.write_text(render_intraday_1m_md(payload), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Wrote {md_path}")
    best = payload.get("best") or {}
    print(
        f"Best {best.get('variant_id')}: excess={best.get('mean_excess_pct')} "
        f"delta={best.get('delta_vs_i0_pp')}pp median_hold={best.get('median_hold_days')} "
        f"paired_p={best.get('paired_delta_p_approx')} ci_lo={best.get('paired_delta_ci95_lo')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
