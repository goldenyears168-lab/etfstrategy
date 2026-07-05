#!/usr/bin/env python3
"""RRG Universe 全檔互動時間軸 · 每日更新至 DB 最新交易日。

Writes:
  length=20 → reports/research/rrg/rrg_universe_timeline_latest.html
  length=5  → reports/research/rrg/rrg_universe_timeline_wma5_latest.html
  + optional dated stamp copy

Usage:
  PYTHONPATH=src python3 scripts/run_rrg_universe_timeline_daily.py
  PYTHONPATH=src python3 scripts/run_rrg_universe_timeline_daily.py --length 5
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from project_config import DEFAULT_ETF_CODES, parse_etf_codes
from project_dotenv import load_project_dotenv
from render_rrg_universe_html import (
    _load_rrg_trajectories,
    _load_trading_dates_range,
    _timeline_year_tag,
    render_universe_timeline_html,
)
from report_paths import research_html_path
from stock_db import DEFAULT_DB_PATH, connect

DEFAULT_DATE_FROM = "2025-01-02"
DEFAULT_LENGTH = 20


def _output_names(length: int, date_from: str, date_to: str, *, stamp: str | None = None) -> tuple[str, str | None]:
    """Return (latest_filename, dated_filename_or_none)."""
    year_tag = _timeline_year_tag(date_from, date_to)
    length_tag = f"wma{length}" if length != DEFAULT_LENGTH else ""
    if length_tag:
        latest = f"rrg_universe_timeline_{length_tag}_latest.html"
        base = f"{stamp or date.today().strftime('%Y%m%d')}_rrg_universe_timeline_{length_tag}"
    else:
        latest = "rrg_universe_timeline_latest.html"
        base = f"{stamp or date.today().strftime('%Y%m%d')}_rrg_universe_timeline"
    dated = f"{base}_{year_tag}.html" if year_tag else f"{base}.html"
    return latest, dated


def _latest_finmind_trade_date(conn) -> str:
    row = conn.execute(
        "SELECT MAX(trade_date) FROM stock_daily_bars WHERE source = 'finmind'"
    ).fetchone()
    if not row or not row[0]:
        raise ValueError("找不到 FinMind 交易日")
    return str(row[0])


def run_rrg_universe_timeline_daily(
    conn,
    *,
    date_from: str = DEFAULT_DATE_FROM,
    date_to: str | None = None,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    length: int = DEFAULT_LENGTH,
    write_dated: bool = True,
) -> dict:
    date_to = date_to or _latest_finmind_trade_date(conn)
    dates = _load_trading_dates_range(conn, date_from=date_from, date_to=date_to)
    if len(dates) < 2:
        raise ValueError(f"至少需要 2 個交易日（{date_from} → {date_to}，實得 {len(dates)}）")

    trajectories = _load_rrg_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        length=length,
        with_close=False,
    )
    if not trajectories:
        raise ValueError(f"{date_from} → {date_to} 無有效 RRG 軌跡")

    html = render_universe_timeline_html(
        dates=dates,
        trajectories=trajectories,
        length=length,
        etf_codes=etf_codes,
    )

    latest_name, dated_name = _output_names(length, date_from, date_to)
    latest = research_html_path("rrg", latest_name)
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(html, encoding="utf-8")

    dated_path: Path | None = None
    if write_dated and dated_name:
        dated_path = research_html_path("rrg", dated_name)
        dated_path.write_text(html, encoding="utf-8")

    return {
        "date_from": date_from,
        "date_to": date_to,
        "length": length,
        "n_dates": len(dates),
        "n_stocks": len(trajectories),
        "report_latest": str(latest),
        "report_dated": str(dated_path) if dated_path else None,
    }


def main() -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="RRG Universe 全檔互動時間軸 · 每日更新")
    parser.add_argument(
        "--date-from",
        default=DEFAULT_DATE_FROM,
        help=f"起始交易日（預設 {DEFAULT_DATE_FROM}）",
    )
    parser.add_argument(
        "--date-to",
        default=None,
        help="結束交易日（預設 DB 最新 FinMind 日）",
    )
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--length", type=int, default=DEFAULT_LENGTH)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--no-dated",
        action="store_true",
        help="僅寫 rrg_universe_timeline_latest.html，不另存當日 stamp 檔",
    )
    args = parser.parse_args()

    etf_codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        result = run_rrg_universe_timeline_daily(
            conn,
            date_from=args.date_from,
            date_to=args.date_to,
            etf_codes=etf_codes,
            length=args.length,
            write_dated=not args.no_dated,
        )
    finally:
        conn.close()

    print(
        f"RRG Universe timeline WMA({result['length']}): {result['date_from']} → {result['date_to']} · "
        f"{result['n_stocks']} stocks · {result['n_dates']} frames"
    )
    print(f"Wrote {result['report_latest']}")
    if result["report_dated"]:
        print(f"Wrote {result['report_dated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
