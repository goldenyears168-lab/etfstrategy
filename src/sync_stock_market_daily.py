#!/usr/bin/env python3
"""
成分股日線 + 三大法人（FinMind）→ stock_daily_bars、stock_institutional_daily。

Universe：各 ETF 最新 snapshot 持股聯集（load_etf_constituent_watchlist）。
同日重跑：已覆蓋窗內 K 線+法人者跳過 API；僅缺尾端者縮短回溯（增量）。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from market_sync_window import min_rows_required, resolve_sync_window
from stock_db import (
    DEFAULT_DB_PATH,
    StockMarketCoverage,
    connect,
    load_etf_constituent_watchlist,
    load_stock_market_coverage_map,
    upsert_stock_daily_bars,
    upsert_stock_institutional_daily,
)
from sync_etf_signal import SOURCE, aggregate_institutional, fetch_finmind

DEFAULT_LOOKBACK_DAYS = 60
REQUEST_DELAY_SEC = 0.35
INCREMENTAL_OVERLAP_DAYS = 7


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(float(value))


def _min_bars_required(lookback_days: int) -> int:
    return min_rows_required(lookback_days)


def resolve_fetch_window(
    coverage: StockMarketCoverage | None,
    start: date,
    end: date,
    lookback_days: int,
    *,
    force_refresh: bool,
) -> tuple[str, date | None, date | None]:
    window_days = max(1, (end - start).days + 1)
    min_bars = _min_bars_required(lookback_days if lookback_days else window_days)
    if coverage is None:
        series: list[tuple[str | None, str | None, int]] = [(None, None, 0), (None, None, 0)]
    else:
        series = [
            (coverage.bar_min, coverage.bar_max, coverage.bar_count_window),
            (coverage.inst_min, coverage.inst_max, coverage.inst_count_window),
        ]
    return resolve_sync_window(
        start=start,
        end=end,
        min_rows=min_bars,
        series=series,
        force_refresh=force_refresh,
    )


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
    lookback_days: int | None = None,
    *,
    window_start: date | None = None,
    window_end: date | None = None,
    stock_ids: list[str] | None = None,
    dry_run: bool = False,
    quiet: bool = False,
    max_stocks: int = 0,
    request_delay: float = REQUEST_DELAY_SEC,
    force_refresh: bool = False,
) -> dict[str, int]:
    end = window_end or date.today()
    if window_start is not None:
        start = window_start
        effective_lookback = max(1, (end - start).days + 1)
    elif lookback_days is not None:
        start = end - timedelta(days=lookback_days)
        effective_lookback = lookback_days
    else:
        effective_lookback = DEFAULT_LOOKBACK_DAYS
        start = end - timedelta(days=effective_lookback)

    conn = connect(db_path)
    try:
        if stock_ids:
            placeholders = ",".join("?" * len(stock_ids))
            name_rows = conn.execute(
                f"""
                SELECT stock_id, MAX(stock_name) AS stock_name
                FROM (
                    SELECT stock_id, stock_name
                    FROM etf_holdings
                    WHERE stock_id IN ({placeholders})
                    UNION ALL
                    SELECT stock_id, stock_name
                    FROM benchmark_constituents
                    WHERE stock_id IN ({placeholders})
                )
                GROUP BY stock_id
                """,
                stock_ids + stock_ids,
            ).fetchall()
            name_by_id = {str(r["stock_id"]): r["stock_name"] or "" for r in name_rows}
            watchlist = [
                {
                    "stock_id": sid,
                    "stock_name": name_by_id.get(sid, ""),
                    "etf_hold_count": 0,
                    "fund_hold_count": 0,
                    "benchmark_hold_count": 0,
                }
                for sid in stock_ids
            ]
        else:
            watchlist = load_etf_constituent_watchlist(conn)
        coverage_stock_ids = [w["stock_id"] for w in watchlist]
        coverage_map = load_stock_market_coverage_map(
            conn,
            coverage_stock_ids,
            window_start=start.isoformat(),
            window_end=end.isoformat(),
        )
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
        "skipped": 0,
        "incremental": 0,
        "full": 0,
    }

    for i, item in enumerate(watchlist):
        stock_id = item["stock_id"]
        action, fetch_start, fetch_end = resolve_fetch_window(
            coverage_map.get(stock_id),
            start,
            end,
            effective_lookback,
            force_refresh=force_refresh,
        )
        if action == "skip":
            stats["skipped"] += 1
            if not quiet:
                cov = coverage_map[stock_id]
                print(
                    f"  SKIP {stock_id}: 已同步 K線至 {cov.bar_max} "
                    f"法人至 {cov.inst_max}（窗內 {cov.bar_count_window} 日）",
                    file=sys.stderr,
                )
            continue

        if i > 0 and request_delay > 0:
            time.sleep(request_delay)

        assert fetch_start is not None and fetch_end is not None
        if action == "incremental":
            stats["incremental"] += 1
        elif action == "backfill":
            stats["full"] += 1
        else:
            stats["full"] += 1

        try:
            bars, institutional = build_stock_rows(stock_id, fetch_start, fetch_end)
            if not bars and not institutional:
                stats["warn"] += 1
                if not quiet:
                    print(f"  WARN {stock_id}: 無 FinMind 資料", file=sys.stderr)
                continue
            stats["ok"] += 1
            if dry_run:
                if not quiet:
                    tag = "增量" if action == "incremental" else "全量"
                    print(
                        f"  DRY {stock_id} ({tag}): bars={len(bars)} inst={len(institutional)} "
                        f"({fetch_start}～{fetch_end})"
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
                tag = "Δ" if action == "incremental" else ""
                print(
                    f"  {stock_id}{tag}: bars={len(bars)} inst={len(institutional)} "
                    f"({fetch_start}～{fetch_end})"
                )
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
            f"跳過 {stats['skipped']} · 增量 {stats['incremental']} · 全量 {stats['full']}，"
            f"upsert bars={stats['bars']} inst={stats['institutional']}，"
            f"warn={stats['warn']}（窗 {start}～{end}）"
        )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="同步成分股日線+法人至 SQLite")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--sync-db", action="store_true", help="寫入 DB（預設僅 dry-run 需另加）")
    parser.add_argument("--dry-run", action="store_true", help="抓取不寫入")
    parser.add_argument("--quiet", action="store_true", help="每檔一行")
    parser.add_argument(
        "--start-date",
        default=None,
        help="明確起始日 YYYY-MM-DD（與 --end-date 搭配；backfill 用）",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="明確結束日 YYYY-MM-DD（預設今天）",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help=f"回溯天數（預設 {DEFAULT_LOOKBACK_DAYS}；與 --start-date 互斥）",
    )
    parser.add_argument("--max-stocks", type=int, default=0, help="0=聯集全部；測試可設 3")
    parser.add_argument(
        "--request-delay",
        type=float,
        default=REQUEST_DELAY_SEC,
        help="每檔間隔秒數，避免 FinMind 限流",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="強制每檔重抓（忽略 DB 覆蓋；易觸發 FinMind 402）",
    )
    args = parser.parse_args()

    if args.start_date and args.lookback_days is not None:
        print("ERROR: --start-date 與 --lookback-days 請擇一", file=sys.stderr)
        return 1

    lookback = args.lookback_days if args.lookback_days is not None else DEFAULT_LOOKBACK_DAYS
    if args.start_date is None and (lookback < 7 or lookback > 730):
        print("lookback-days 建議 30～90（允許 7～730）", file=sys.stderr)

    window_start = date.fromisoformat(args.start_date) if args.start_date else None
    window_end = date.fromisoformat(args.end_date) if args.end_date else None

    dry_run = args.dry_run or not args.sync_db
    try:
        sync_stock_market_daily(
            args.db,
            lookback if window_start is None else None,
            window_start=window_start,
            window_end=window_end,
            dry_run=dry_run,
            quiet=args.quiet,
            max_stocks=args.max_stocks,
            request_delay=args.request_delay,
            force_refresh=args.force_refresh,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
