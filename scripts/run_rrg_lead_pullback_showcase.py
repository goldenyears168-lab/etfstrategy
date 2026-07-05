#!/usr/bin/env python3
"""強勢回踩 · pool1-style 回測 showcase HTML。

Usage:
  PYTHONPATH=src:scripts python3 scripts/run_rrg_lead_pullback_showcase.py
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
from render_lead_pullback_backtest_showcase_html import (
    load_showcase_timeline_inputs,
    render_lead_pullback_backtest_showcase_html,
)
from render_rrg_universe_html import _load_trading_dates_range
from report_paths import research_html_path
from research.backtest.lead_pullback_showcase import build_lead_pullback_showcase_bundle
from rrg_mono_daily_brief import LENGTH
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_DATE_FROM = "2026-04-01"
DEFAULT_DATE_TO = "2026-07-03"
OUTPUT_NAME = "rrg_lead_pullback_showcase_20260401_20260703.html"


def main() -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="強勢回踩 · 回測 showcase HTML")
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
        if len(dates) < 5:
            raise ValueError(f"need ≥5 trade dates, got {len(dates)}")

        t0 = time.perf_counter()
        bundle = build_lead_pullback_showcase_bundle(
            conn,
            dates,
            n_slots=args.n_slots,
            capital_ntd=args.capital_ntd,
        )
        trajectories, bench_closes = load_showcase_timeline_inputs(
            conn,
            bundle,
            dates,
            etf_codes=etf_codes,
            length=args.length,
        )
        html = render_lead_pullback_backtest_showcase_html(
            bundle=bundle,
            dates=dates,
            all_trajectories=trajectories,
            bench_closes=bench_closes,
            length=args.length,
        )
        elapsed = time.perf_counter() - t0
    finally:
        conn.close()

    out = args.output or research_html_path("rrg", OUTPUT_NAME)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    cmp_ = bundle.get("comparison") or {}
    print(
        f"Lead pullback showcase: {args.date_from} → {args.date_to} · "
        f"{len(dates)} TD · "
        f"LPB {_fmt(cmp_.get('lpb_total_return_pct'))}% vs S2 {_fmt(cmp_.get('s2_total_return_pct'))}% · "
        f"legs {cmp_.get('lpb_n_executed')} · {elapsed:.1f}s"
    )
    print(f"Wrote {out}")
    return 0


def _fmt(v) -> str:
    if v is None:
        return "—"
    return f"{float(v):+.2f}"


if __name__ == "__main__":
    raise SystemExit(main())
