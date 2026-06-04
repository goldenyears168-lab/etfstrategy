#!/usr/bin/env python3
"""
成分股日線 + 三大法人（FinMind）→ stock_daily_bars、stock_institutional_daily。

Universe：各 ETF 最新 snapshot 持股聯集（load_etf_constituent_watchlist）。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    load_etf_constituent_watchlist,
    upsert_stock_daily_bars,
    upsert_stock_institutional_daily,
)
from sync_etf_signal import SOURCE, aggregate_institutional, fetch_finmind

DEFAULT_LOOKBACK_DAYS = 60
REQUEST_DELAY_SEC = 0.35


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(float(value))


def build_stock_rows(
    stock_id: str,
    start: date,
    end: date,
) -> tuple[list[dict], list[dict]]:
    price_rows = fetch_finmind("TaiwanStockPrice", stock_id, start, end)
    bars: list[dict] = []
    close_by_date: dict[str, float] = {}
    for row in price_rows:
        trade_date = str(row["date"])[:10]
        close = float(row["close"])
        close_by_date[trade_date] = close
        bars.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "open": _float_or_none(row.get("open")),
                "high": _float_or_none(row.get("max")),
                "low": _float_or_none(row.get("min")),
                "close": close,
                "volume": _int_or_none(row.get("Trading_Volume") or row.get("volume")),
                "source": SOURCE,
            }
        )

    inst_rows = fetch_finmind("TaiwanStockInstitutionalInvestorsBuySell", stock_id, start, end)
    inst_by_date = aggregate_institutional(inst_rows)
    institutional: list[dict] = []
    for trade_date in sorted(inst_by_date):
        nets = inst_by_date[trade_date]
        institutional.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "close_price": close_by_date.get(trade_date),
                "foreign_net": nets["foreign_net"],
                "investment_trust_net": nets["investment_trust_net"],
                "dealer_self_net": nets["dealer_self_net"],
                "three_institution_net": nets["three_institution_net"],
                "source": SOURCE,
            }
        )
    return bars, institutional


def sync_stock_market_daily(
    db_path: Path,
    lookback_days: int,
    *,
    dry_run: bool = False,
    quiet: bool = False,
    max_stocks: int = 0,
    request_delay: float = REQUEST_DELAY_SEC,
) -> dict[str, int]:
    end = date.today()
    start = end - timedelta(days=lookback_days)

    conn = connect(db_path)
    try:
        watchlist = load_etf_constituent_watchlist(conn)
    finally:
        conn.close()

    if not watchlist:
        raise RuntimeError("持股聯集為空：請先跑收盤持股同步寫入 etf_holdings")

    if max_stocks > 0:
        watchlist = watchlist[:max_stocks]

    stats = {
        "stocks": len(watchlist),
        "bars": 0,
        "institutional": 0,
        "ok": 0,
        "warn": 0,
    }

    for i, item in enumerate(watchlist):
        stock_id = item["stock_id"]
        if i > 0 and request_delay > 0:
            time.sleep(request_delay)
        try:
            bars, institutional = build_stock_rows(stock_id, start, end)
            if not bars and not institutional:
                stats["warn"] += 1
                if not quiet:
                    print(f"  WARN {stock_id}: 無 FinMind 資料", file=sys.stderr)
                continue
            stats["ok"] += 1
            if dry_run:
                if not quiet:
                    print(
                        f"  DRY {stock_id}: bars={len(bars)} inst={len(institutional)} "
                        f"({start}～{end})"
                    )
                stats["bars"] += len(bars)
                stats["institutional"] += len(institutional)
                continue
            conn = connect(db_path)
            try:
                stats["bars"] += upsert_stock_daily_bars(conn, bars)
                stats["institutional"] += upsert_stock_institutional_daily(conn, institutional)
            finally:
                conn.close()
            if quiet:
                print(f"  {stock_id}: bars={len(bars)} inst={len(institutional)}")
        except requests.HTTPError as exc:
            stats["warn"] += 1
            print(f"  WARN {stock_id}: FinMind HTTP {exc}", file=sys.stderr)
        except RuntimeError as exc:
            stats["warn"] += 1
            print(f"  WARN {stock_id}: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            stats["warn"] += 1
            print(f"  WARN {stock_id}: {exc}", file=sys.stderr)

    if not quiet and not dry_run:
        print(
            f"成分股市場 sync：{stats['ok']}/{stats['stocks']} 檔 OK，"
            f"upsert bars={stats['bars']} inst={stats['institutional']}，"
            f"warn={stats['warn']}（{start}～{end}）"
        )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="同步成分股日線+法人至 SQLite")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--sync-db", action="store_true", help="寫入 DB（預設僅 dry-run 需另加）")
    parser.add_argument("--dry-run", action="store_true", help="抓取不寫入")
    parser.add_argument("--quiet", action="store_true", help="每檔一行")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"回溯天數（預設 {DEFAULT_LOOKBACK_DAYS}，建議 30～90）",
    )
    parser.add_argument("--max-stocks", type=int, default=0, help="0=聯集全部；測試可設 3")
    parser.add_argument(
        "--request-delay",
        type=float,
        default=REQUEST_DELAY_SEC,
        help="每檔間隔秒數，避免 FinMind 限流",
    )
    args = parser.parse_args()

    if args.lookback_days < 7 or args.lookback_days > 120:
        print("lookback-days 建議 30～90（允許 7～120）", file=sys.stderr)

    dry_run = args.dry_run or not args.sync_db
    try:
        sync_stock_market_daily(
            args.db,
            args.lookback_days,
            dry_run=dry_run,
            quiet=args.quiet,
            max_stocks=args.max_stocks,
            request_delay=args.request_delay,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
