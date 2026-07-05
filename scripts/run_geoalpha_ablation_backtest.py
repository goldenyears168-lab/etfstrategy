#!/usr/bin/env python3
"""GeoAlpha Phase 2 · M0–M5 ablation backtest."""

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
from render_rrg_universe_html import _load_trading_dates_range
from research.backtest.geoalpha_ablation_backtest import (
    ALL_MODELS,
    DEFAULT_STRUCT_PERCENTILE,
    run_geoalpha_ablation,
    write_geoalpha_ablation_report,
)
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_DATE_FROM = "2026-04-01"
DEFAULT_DATE_TO = "2026-07-03"


def main() -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser(description="GeoAlpha M0–M5 ablation backtest")
    ap.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    ap.add_argument("--date-to", default=DEFAULT_DATE_TO)
    ap.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--models",
        default=",".join(ALL_MODELS),
        help="Comma-separated model ids (default M0,M1,...,M5)",
    )
    ap.add_argument(
        "--struct-percentile",
        type=int,
        default=DEFAULT_STRUCT_PERCENTILE,
        help="Struct gate percentile (70, 50, 0=disabled)",
    )
    args = ap.parse_args()

    models = tuple(m.strip().upper() for m in args.models.split(",") if m.strip())
    etf_codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        dates = _load_trading_dates_range(conn, date_from=args.date_from, date_to=args.date_to)
        if not dates:
            raise ValueError(f"無交易日 {args.date_from} → {args.date_to}")

        t0 = time.perf_counter()
        payload = run_geoalpha_ablation(
            conn,
            dates,
            etf_codes,
            models=models,
            struct_percentile=args.struct_percentile,
        )
        elapsed = time.perf_counter() - t0
    finally:
        conn.close()

    json_path, md_path = write_geoalpha_ablation_report(payload)

    print(f"GeoAlpha ablation: {args.date_from} → {args.date_to} · {elapsed:.1f}s")
    for r in payload.get("rows") or []:
        vs = r.get("vs_m0_pp")
        vs_s = f" vs M0 {vs:+.3f}pp" if vs is not None else ""
        print(
            f"  {r['model']}: n={r.get('n')} excess={r.get('mean_excess_pct')}%{vs_s}"
        )
    v = payload.get("verdict") or {}
    print(f"  Verdict: {'PASS' if v.get('all_expected') else 'MIXED'} · {v.get('recommendation')}")
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
