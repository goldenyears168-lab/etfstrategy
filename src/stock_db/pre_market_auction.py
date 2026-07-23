"""盤前試撮／委買委賣 snapshot storage（研究用 · 累積歷史庫供未來回測）。

Populated by ``scripts/research/collect_pre_market_auction_snapshot.py``。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PreMarketAuctionSnapshotRow:
    stock_id: str
    trade_date: str
    snapshot_ts: str
    bid_prices: list[float] | None
    bid_volumes: list[float] | None
    ask_prices: list[float] | None
    ask_volumes: list[float] | None
    match_price: float | None
    match_volume: float | None
    is_locked: bool
    source: str
    synced_at: str


def upsert_pre_market_auction_snapshot(
    conn: sqlite3.Connection,
    rows: list[PreMarketAuctionSnapshotRow],
) -> int:
    """Idempotent upsert；PK=(stock_id, trade_date, snapshot_ts, source)。"""
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO pre_market_auction_snapshot (
            stock_id, trade_date, snapshot_ts, bid_prices, bid_volumes,
            ask_prices, ask_volumes, match_price, match_volume, is_locked,
            source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :snapshot_ts, :bid_prices, :bid_volumes,
            :ask_prices, :ask_volumes, :match_price, :match_volume, :is_locked,
            :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, snapshot_ts, source) DO UPDATE SET
            bid_prices=excluded.bid_prices,
            bid_volumes=excluded.bid_volumes,
            ask_prices=excluded.ask_prices,
            ask_volumes=excluded.ask_volumes,
            match_price=excluded.match_price,
            match_volume=excluded.match_volume,
            is_locked=excluded.is_locked,
            synced_at=excluded.synced_at
        """,
        [
            {
                "stock_id": r.stock_id,
                "trade_date": r.trade_date,
                "snapshot_ts": r.snapshot_ts,
                "bid_prices": json.dumps(r.bid_prices, ensure_ascii=False)
                if r.bid_prices is not None
                else None,
                "bid_volumes": json.dumps(r.bid_volumes, ensure_ascii=False)
                if r.bid_volumes is not None
                else None,
                "ask_prices": json.dumps(r.ask_prices, ensure_ascii=False)
                if r.ask_prices is not None
                else None,
                "ask_volumes": json.dumps(r.ask_volumes, ensure_ascii=False)
                if r.ask_volumes is not None
                else None,
                "match_price": r.match_price,
                "match_volume": r.match_volume,
                "is_locked": 1 if r.is_locked else 0,
                "source": r.source,
                "synced_at": r.synced_at,
            }
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)
