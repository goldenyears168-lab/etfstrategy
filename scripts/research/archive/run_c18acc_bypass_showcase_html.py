#!/usr/bin/env python3
"""POOL1 vs POOL1+bypass leg diff showcase · poll_5m · full window default."""

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
from render_c18acc_pool1_showcase_html import (
    load_showcase_timeline_inputs,
    render_bypass_showcase_html,
)
from render_rrg_universe_html import _load_trading_dates_range
from report_paths import research_html_path
from research.backtest.archive.c18acc_pool1_showcase import build_bypass_showcase_bundle
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_DATE_FROM = "2025-01-02"
DEFAULT_OUTPUT = "20260705_c18acc_pool1_bypass_showcase_full.html"


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser(description="POOL1 vs POOL1+bypass leg diff showcase HTML")
    ap.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    ap.add_argument("--date-to", default=None, help="default: DB latest")
    ap.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    ap.add_argument("--length", type=int, default=20)
    ap.add_argument("--n-slots", type=int, default=3)
    ap.add_argument("--capital-ntd", type=float, default=10_000.0)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args(argv)

    etf_codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        dates = _load_trading_dates_range(
            conn, date_from=args.date_from, date_to=args.date_to
        )
        if len(dates) < 5:
            raise ValueError(f"need ≥5 trade dates, got {len(dates)}")

        t0 = time.perf_counter()
        bundle = build_bypass_showcase_bundle(
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
        html = render_bypass_showcase_html(
            bundle=bundle,
            dates=dates,
            all_trajectories=trajectories,
            bench_closes=bench_closes,
            length=args.length,
        )
        out = args.output or research_html_path("rrg", DEFAULT_OUTPUT)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        elapsed = time.perf_counter() - t0
    finally:
        conn.close()

    cmp_ = bundle.get("comparison") or {}
    print(
        f"bypass showcase: {dates[0]} → {dates[-1]} · "
        f"{len(dates)} TD · Δexcess={cmp_.get('delta_excess_pp')}pp · "
        f"Δswaps={cmp_.get('delta_swaps')} · "
        f"★bypass_only={len(bundle.get('bypass_only_legs') or [])} · "
        f"base_only={len(bundle.get('base_only_legs') or [])} · {elapsed:.1f}s"
    )
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
