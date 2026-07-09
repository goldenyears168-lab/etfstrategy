#!/usr/bin/env python3
"""全宇宙盤中定池 sweep · 比較 PIT fresh mono vs 盤中替代條件。

用法：
  PYTHONPATH=src .venv/bin/python scripts/run_intraday_universe_pool_sweep.py
  PYTHONPATH=src .venv/bin/python scripts/run_intraday_universe_pool_sweep.py \\
    --date-start 2024-06-01 --date-end 2026-06-26 --case-date 2026-06-30
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv
from report_paths import RESEARCH_RRG
from research.backtest.archive.intraday_universe_pool_sweep import (
    diagnose_session_tick,
    render_sweep_md,
    run_intraday_universe_pool_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect


def main() -> int:
    load_project_dotenv()
    p = argparse.ArgumentParser(description="Intraday universe pool sweep")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--date-start", default="2024-06-01")
    p.add_argument("--date-end", default="2026-06-26")
    p.add_argument("--min-kbar-stocks", type=int, default=80)
    p.add_argument("--case-date", default="2026-06-30", help="Tick 診斷日（如 2645 案例）")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    conn = connect(args.db)
    try:
        payload = run_intraday_universe_pool_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            min_kbar_stocks=args.min_kbar_stocks,
        )
        case = diagnose_session_tick(conn, args.case_date)
    finally:
        conn.close()

    payload["case_study"] = case
    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_intraday_universe_pool_sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out.with_suffix(".md")
    md_path.write_text(render_sweep_md(payload, case_study=case), encoding="utf-8")

    print(f"Wrote {out}")
    print(f"Wrote {md_path}")
    print(f"\nSessions: {payload.get('n_sessions')}")
    print("\nTop 8 by hold1 mean excess:")
    for row in (payload.get("ranked") or [])[:8]:
        print(
            f"  {row['key']:40s}  alerts/day={row.get('mean_alerts_per_day')}  "
            f"hold1={row.get('hold1_mean_excess')}pp  win={row.get('hold1_win_pct')}%"
        )
    print(f"\nCase {args.case_date} (tick):")
    for r in case.get("rows") or []:
        flags = []
        if r.get("pit_fresh_mono"):
            flags.append("PIT-fresh")
        if r.get("intraday_fresh_mono"):
            flags.append("ID-fresh")
        if r.get("intraday_mono_up_fresh"):
            flags.append("ID-mu-fresh")
        print(f"  {r['stock_id']}: {', '.join(flags) or '—'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
