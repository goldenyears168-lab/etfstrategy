#!/usr/bin/env python3
"""
Sponsor 獨有籌碼：分點聚合 + 鉅額交易（Top N 研究池）。

分點使用 TaiwanStockTradingDailyReportSecIdAgg（標準 /data 端點）。
鉅額使用 TaiwanStockBlockTrade。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from finmind_client import fetch_finmind
from project_config import DEFAULT_TOP_N, SCORE_VERSION
from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    load_latest_pm_watchlist,
    upsert_stock_block_trade,
    upsert_stock_branch_daily,
)
from sync_etf_signal import SOURCE

DEFAULT_LOOKBACK_DAYS = 30
REQUEST_DELAY_SEC = 0.5
SMART_BRANCH_KEYWORDS = ("美林", "摩根", "高盛", "瑞銀", "花旗", "港商", "美商")
RETAIL_BRANCH_KEYWORDS = ("凱基", "元大", "富邦", "國泰", "群益", "永豐", "玉山")


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _branch_bucket(name: str) -> str:
    for kw in SMART_BRANCH_KEYWORDS:
        if kw in name:
            return "smart"
    for kw in RETAIL_BRANCH_KEYWORDS:
        if kw in name:
            return "retail"
    return "other"


def parse_branch_agg_rows(stock_id: str, raw: list[dict]) -> list[dict]:
    """依交易日聚合分點買賣（SecIdAgg）。"""
    by_date: dict[str, list[dict]] = {}
    for item in raw:
        trade_date = str(item.get("date") or item.get("Date") or "")[:10]
        if not trade_date:
            continue
        by_date.setdefault(trade_date, []).append(item)

    rows: list[dict] = []
    for trade_date, items in sorted(by_date.items()):
        nets: list[tuple[str, float]] = []
        smart_net = 0.0
        retail_net = 0.0
        for it in items:
            name = str(it.get("securities_trader") or it.get("name") or "")
            buy = _float_or_none(it.get("buy") or it.get("Buy")) or 0.0
            sell = _float_or_none(it.get("sell") or it.get("Sell")) or 0.0
            net = buy - sell
            nets.append((name, net))
            bucket = _branch_bucket(name)
            if bucket == "smart":
                smart_net += net
            elif bucket == "retail":
                retail_net += net
        nets.sort(key=lambda x: x[1], reverse=True)
        buy_top5 = sum(n for _, n in nets[:5] if n > 0)
        sell_top5 = sum(n for _, n in sorted(nets, key=lambda x: x[1])[:5] if n < 0)
        rows.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "buy_top5_net": buy_top5,
                "sell_top5_net": sell_top5,
                "smart_net": smart_net,
                "retail_net": retail_net,
                "branch_count": len(items),
                "source": SOURCE,
            }
        )
    return rows


def parse_block_trade_rows(stock_id: str, raw: list[dict]) -> list[dict]:
    by_date: dict[str, list[dict]] = {}
    for item in raw:
        trade_date = str(item.get("date") or item.get("Date") or "")[:10]
        if not trade_date:
            continue
        by_date.setdefault(trade_date, []).append(item)
    rows: list[dict] = []
    for trade_date, items in sorted(by_date.items()):
        vol = 0.0
        amt = 0.0
        for it in items:
            v = _float_or_none(it.get("volume") or it.get("Volume")) or 0.0
            a = _float_or_none(it.get("amount") or it.get("Amount")) or 0.0
            vol += v
            amt += a
        rows.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "block_volume": vol,
                "block_amount": amt,
                "block_count": len(items),
                "source": SOURCE,
            }
        )
    return rows


def resolve_top_stock_ids(
    conn,
    *,
    top_n: int,
) -> list[str]:
    pm = load_latest_pm_watchlist(conn, score_version=SCORE_VERSION)
    if not pm:
        return []
    ranked = sorted(
        pm,
        key=lambda r: float(r["investment_score"] or 0),
        reverse=True,
    )
    return [str(r["stock_id"]) for r in ranked[:top_n]]


def sync_stock_sponsor_daily(
    db_path: Path,
    *,
    stock_ids: list[str],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    dry_run: bool = False,
    quiet: bool = False,
    request_delay: float = REQUEST_DELAY_SEC,
) -> dict[str, int]:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    stats = {"branch": 0, "block": 0, "ok": 0, "warn": 0}

    for i, stock_id in enumerate(stock_ids):
        if i > 0 and request_delay > 0:
            time.sleep(request_delay)
        try:
            branch_raw = fetch_finmind(
                "TaiwanStockTradingDailyReportSecIdAgg",
                stock_id,
                start,
                end,
            )
            block_raw = fetch_finmind("TaiwanStockBlockTrade", stock_id, start, end)
            branch = parse_branch_agg_rows(stock_id, branch_raw)
            block = parse_block_trade_rows(stock_id, block_raw)
            if not branch and not block:
                stats["warn"] += 1
                continue
            stats["ok"] += 1
            if dry_run:
                stats["branch"] += len(branch)
                stats["block"] += len(block)
                continue
            conn = connect(db_path)
            try:
                stats["branch"] += upsert_stock_branch_daily(conn, branch)
                stats["block"] += upsert_stock_block_trade(conn, block)
            finally:
                conn.close()
            if quiet:
                print(f"  {stock_id}: branch={len(branch)} block={len(block)}")
        except (requests.HTTPError, RuntimeError, Exception) as exc:  # noqa: BLE001
            stats["warn"] += 1
            print(f"  WARN {stock_id}: {exc}", file=sys.stderr)

    if not quiet and not dry_run:
        print(
            f"Sponsor 籌碼 sync：{stats['ok']}/{len(stock_ids)} OK，"
            f"branch={stats['branch']} block={stats['block']} warn={stats['warn']}"
        )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Sponsor 分點+鉅額（Top N）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--sync-db", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        stock_ids = resolve_top_stock_ids(conn, top_n=args.top_n)
    finally:
        conn.close()
    if not stock_ids:
        print("ERROR: 無 pm_watchlist；請先跑 Score Engine", file=sys.stderr)
        return 1

    dry_run = args.dry_run or not args.sync_db
    sync_stock_sponsor_daily(
        args.db,
        stock_ids=stock_ids,
        lookback_days=args.lookback_days,
        dry_run=dry_run,
        quiet=args.quiet,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
