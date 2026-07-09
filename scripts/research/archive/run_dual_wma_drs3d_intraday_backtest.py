#!/usr/bin/env python3
"""H-A1 · Dual WMA intraday spread score vs drs_rest / drs_3d backtest."""

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
from report_paths import RESEARCH_RRG
from research.backtest.archive.dual_wma_drs3d_intraday_backtest import (
    run_h_a1_backtest,
    write_h_a1_report,
)
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_DATE_FROM = "2026-04-01"
DEFAULT_DATE_TO = "2026-07-03"


def main() -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser(description="H-A1 dual WMA intraday drs backtest")
    ap.add_argument("--date-from", default=DEFAULT_DATE_FROM)
    ap.add_argument("--date-to", default=DEFAULT_DATE_TO)
    ap.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--out-dir", type=Path, default=RESEARCH_RRG)
    ap.add_argument(
        "--no-strict-extension",
        action="store_true",
        help="Use classify imp_extension (not strict spread_rs>0)",
    )
    args = ap.parse_args()

    etf_codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        dates = _load_trading_dates_range(
            conn, date_from=args.date_from, date_to=args.date_to
        )
        if not dates:
            raise ValueError(f"無交易日 {args.date_from} → {args.date_to}")

        t0 = time.perf_counter()
        result = run_h_a1_backtest(
            conn,
            dates=dates,
            etf_codes=etf_codes,
            strict_extension=not args.no_strict_extension,
        )
        elapsed = time.perf_counter() - t0
    finally:
        conn.close()

    json_path, md_path = write_h_a1_report(result, args.out_dir)
    summ = result.get("summary", {})
    ext_ic = next(
        (r for r in result.get("ic_by_signal", []) if r["signal"] == "imp_extension"),
        {},
    )
    print(
        f"H-A1 backtest: {args.date_from} → {args.date_to} · {elapsed:.1f}s · "
        f"events={summ.get('n_events_total', 0)}"
    )
    print(
        f"  imp_extension n={summ.get('n_imp_extension', 0)} · "
        f"IC_rest intraday={ext_ic.get('ic_intraday_vs_drs_rest')} "
        f"daily={ext_ic.get('ic_daily_vs_drs_rest')} · "
        f"mean_ret_3d_net={ext_ic.get('mean_ret_3d_net')}%"
    )
    print(f"  H-A1 pass (intraday IC > daily IC on rest): {result.get('h_a1_intraday_beats_daily_rest')}")
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
