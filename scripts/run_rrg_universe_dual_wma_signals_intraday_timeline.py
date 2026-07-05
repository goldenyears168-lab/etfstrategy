#!/usr/bin/env python3
"""RRG Universe 雙 WMA 四情境訊號盤中時間軸。

Usage:
  PYTHONPATH=src python3 scripts/run_rrg_universe_dual_wma_signals_intraday_timeline.py
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from project_config import DEFAULT_ETF_CODES, parse_etf_codes
from project_dotenv import load_project_dotenv
from render_rrg_dual_wma_signals_html import render_universe_dual_wma_signals_intraday_html
from render_rrg_universe_html import _load_trading_dates_range
from report_paths import research_html_path
from rrg_universe_intraday_panel import (
    WMA_LONG_LENGTH,
    WMA_SHORT_LENGTH,
    build_dual_wma_intraday_trajectories,
)
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_DATE_FROM = "2026-04-01"
DEFAULT_DATE_TO = "2026-07-03"
OUTPUT_NAME = "rrg_universe_timeline_dual_wma_signals_intraday_20260401_20260703.html"


def main() -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="RRG 四情境訊號盤中時間軸")
    parser.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    parser.add_argument("--date-to", default=DEFAULT_DATE_TO)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--short-length", type=int, default=WMA_SHORT_LENGTH)
    parser.add_argument("--long-length", type=int, default=WMA_LONG_LENGTH)
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
        frames, trajectories, meta = build_dual_wma_intraday_trajectories(
            conn,
            dates=dates,
            etf_codes=etf_codes,
            short_length=args.short_length,
            long_length=args.long_length,
        )
        elapsed = time.perf_counter() - t0
        if not trajectories:
            raise ValueError("無有效雙 WMA 盤中軌跡")

        html = render_universe_dual_wma_signals_intraday_html(
            frames=frames,
            trajectories=trajectories,
            etf_codes=etf_codes,
            meta=meta,
            short_length=args.short_length,
            long_length=args.long_length,
        )
        out = args.output or research_html_path("rrg", OUTPUT_NAME)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
    finally:
        conn.close()

    print(
        f"RRG dual WMA signals: {args.date_from} → {args.date_to} · "
        f"{meta['n_trajectories']} stocks · {meta['n_frames']} frames · {elapsed:.1f}s"
    )
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
