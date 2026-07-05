#!/usr/bin/env python3
"""補齊 signal radar 賣方宇宙 · ETF 監測清單 1m K（Yahoo Chart · 近端盤中）。"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from backfill_rrg_lens_backtest_data import (  # noqa: E402
    _parse_date,
    _trade_dates_in_range,
    backfill_stock_kbar_yahoo,
)
from project_config import DEFAULT_ETF_CODES  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect, load_etf_constituent_watchlist  # noqa: E402
from stock_db.kbar import kbar_day_has_data  # noqa: E402


def coverage_report(
    conn,
    *,
    trade_dates: list[str],
    stock_ids: list[str],
) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in trade_dates:
        out[d] = sum(1 for sid in stock_ids if kbar_day_has_data(conn, sid, d))
    return out


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    p = argparse.ArgumentParser(description="Backfill 1m kbar for signal radar sell universe")
    p.add_argument("--start", metavar="YYYY-MM-DD", help="開始日（含）")
    p.add_argument("--end", metavar="YYYY-MM-DD", help="結束日（含）")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="單日（等同 start=end）")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH))
    p.add_argument("--force", action="store_true", help="略過已有資料檢查")
    p.add_argument("--delay", type=float, default=0.35, help="Yahoo 請求間隔秒")
    p.add_argument("--report-only", action="store_true", help="只報告覆蓋率")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    if args.date:
        start_s, end_s = args.date, args.date
    elif args.start and args.end:
        start_s, end_s = args.start, args.end
    else:
        p.error("需要 --date 或 --start/--end")

    conn = connect(Path(args.db))
    try:
        stock_ids = sorted(
            {str(w["stock_id"]) for w in load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES) if w.get("stock_id")}
        )
        trade_dates = _trade_dates_in_range(conn, start=start_s, end=end_s)
        if not trade_dates:
            print(f"無交易日 in {start_s}..{end_s}")
            return 1

        before = coverage_report(conn, trade_dates=trade_dates, stock_ids=stock_ids)
        print(f"=== signal radar kbar · watchlist {len(stock_ids)} 檔 ===")
        for d, n in before.items():
            print(f"  {d}: {n}/{len(stock_ids)} 有 1m K")

        if args.report_only:
            return 0

        start_d = _parse_date(start_s)
        end_d = _parse_date(end_s)
        print(f"--- Yahoo 1m backfill {start_s}..{end_s} ---")
        n = backfill_stock_kbar_yahoo(
            conn,
            start=start_d,
            end=end_d,
            stock_ids=stock_ids,
            force=args.force,
            delay=max(0.2, args.delay),
            quiet=args.quiet,
            kbar_1m_only=True,
            recent_days=max(1, (end_d - start_d).days + 1),
        )
        print(f"bars_upserted={n}")

        after = coverage_report(conn, trade_dates=trade_dates, stock_ids=stock_ids)
        print("=== after ===")
        for d, n in after.items():
            print(f"  {d}: {n}/{len(stock_ids)} 有 1m K")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
