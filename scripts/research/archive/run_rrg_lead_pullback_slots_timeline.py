#!/usr/bin/env python3
"""強勢回踩 · 3槽回測 RRG 時間軸（交易紀錄 · 價格 · 累計報酬）。

Usage:
  PYTHONPATH=src:scripts python3 scripts/run_rrg_lead_pullback_slots_timeline.py
  PYTHONPATH=src:scripts python3 scripts/run_rrg_lead_pullback_slots_timeline.py \\
    --date-from 2025-01-02 --date-to 2026-07-03
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_config import DEFAULT_ETF_CODES, parse_etf_codes
from project_dotenv import load_project_dotenv
from render_rrg_universe_html import (
    _build_lead_pullback_executed_legs,
    _load_bench_closes_for_dates,
    _load_rrg_trajectories,
    _load_trading_dates_range,
    render_l1h9_slots_timeline_html,
)
from report_paths import research_html_path
from rrg_mono_daily_brief import LENGTH
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_DATE_FROM = "2026-04-01"
DEFAULT_DATE_TO = "2026-07-03"
OUTPUT_NAME = "rrg_lead_pullback_slots_timeline_20260401_20260703.html"


def main() -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="強勢回踩 3槽回測 RRG 時間軸")
    parser.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    parser.add_argument("--date-to", default=DEFAULT_DATE_TO)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--n-slots", type=int, default=3)
    parser.add_argument("--capital-ntd", type=float, default=10_000.0)
    parser.add_argument("--length", type=int, default=LENGTH)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    etf_codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        dates = _load_trading_dates_range(
            conn, date_from=args.date_from, date_to=args.date_to
        )
        if not dates:
            raise ValueError(f"無交易日 {args.date_from} → {args.date_to}")

        t0 = time.perf_counter()
        legs, executed, skipped, meta = _build_lead_pullback_executed_legs(
            conn,
            dates,
            n_slots=args.n_slots,
            capital_ntd=args.capital_ntd,
        )
        if not legs:
            raise ValueError("無可執行 leg")

        bench_closes = _load_bench_closes_for_dates(conn, dates)
        all_trajectories = _load_rrg_trajectories(
            conn,
            dates=dates,
            etf_codes=etf_codes,
            length=args.length,
            with_close=True,
        )
        html = render_l1h9_slots_timeline_html(
            etf_code=meta.get("display_code") or "LPB",
            dates=dates,
            legs=legs,
            executed_signals=executed,
            skipped_signals=skipped,
            all_trajectories=all_trajectories,
            meta=meta,
            bench_closes=bench_closes,
            length=args.length,
        )
        elapsed = time.perf_counter() - t0
    finally:
        conn.close()

    if args.output:
        out = args.output
    elif args.date_from == DEFAULT_DATE_FROM and args.date_to == DEFAULT_DATE_TO:
        out = research_html_path("rrg", OUTPUT_NAME)
    else:
        tag = f"{args.date_from.replace('-', '')}_{args.date_to.replace('-', '')}"
        out = research_html_path("rrg", f"rrg_lead_pullback_slots_timeline_{tag}.html")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    print(
        f"Lead pullback slots: {args.date_from} → {args.date_to} · "
        f"{meta['n_executed']}/{meta['n_signals']} executed · "
        f"skipped {meta['n_skipped']} · {elapsed:.1f}s"
    )
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
