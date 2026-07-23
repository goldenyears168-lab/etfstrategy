"""富邦 Neo API（底層 Fugle marketdata）盤前試撮 quote snapshot storage（研究用）。

跟 ``pre_market_auction_snapshot``（FinMind 來源，已證實盤前 08:30-09:00 無真實訊號，
2026-07-22 停用）不同：這個來源的 quote() 回應內含 ``lastTrial``（試撮成交，含
price/size/serial/time）跟五檔 ``bids``/``asks``，是券商直連交易所的即時 feed，
不是 FinMind 那種延遲彙總。

Populated by ``scripts/research/collect_fubon_premarket_quote.py``。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class FubonPremarketQuoteRow:
    stock_id: str
    trade_date: str
    poll_ts: str
    bid_prices: list[float] | None
    bid_sizes: list[float] | None
    ask_prices: list[float] | None
    ask_sizes: list[float] | None
    last_trade_price: float | None
    last_trade_size: float | None
    last_trade_serial: int | None
    last_trade_ts: str | None
    last_trial_price: float | None
    last_trial_size: float | None
    last_trial_serial: int | None
    last_trial_ts: str | None
    is_open: bool | None
    is_open_delayed: bool | None
    is_close: bool | None
    is_close_delayed: bool | None
    raw_json: str | None
    source: str
    synced_at: str


def _bool_to_int(val: bool | None) -> int | None:
    return None if val is None else (1 if val else 0)


def upsert_fubon_premarket_quote(
    conn: sqlite3.Connection,
    rows: list[FubonPremarketQuoteRow],
) -> int:
    """Idempotent upsert；PK=(stock_id, trade_date, poll_ts, source)。"""
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO fubon_premarket_quote_snapshot (
            stock_id, trade_date, poll_ts, bid_prices, bid_sizes,
            ask_prices, ask_sizes, last_trade_price, last_trade_size,
            last_trade_serial, last_trade_ts, last_trial_price, last_trial_size,
            last_trial_serial, last_trial_ts, is_open, is_open_delayed,
            is_close, is_close_delayed, raw_json, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :poll_ts, :bid_prices, :bid_sizes,
            :ask_prices, :ask_sizes, :last_trade_price, :last_trade_size,
            :last_trade_serial, :last_trade_ts, :last_trial_price, :last_trial_size,
            :last_trial_serial, :last_trial_ts, :is_open, :is_open_delayed,
            :is_close, :is_close_delayed, :raw_json, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, poll_ts, source) DO UPDATE SET
            bid_prices=excluded.bid_prices,
            bid_sizes=excluded.bid_sizes,
            ask_prices=excluded.ask_prices,
            ask_sizes=excluded.ask_sizes,
            last_trade_price=excluded.last_trade_price,
            last_trade_size=excluded.last_trade_size,
            last_trade_serial=excluded.last_trade_serial,
            last_trade_ts=excluded.last_trade_ts,
            last_trial_price=excluded.last_trial_price,
            last_trial_size=excluded.last_trial_size,
            last_trial_serial=excluded.last_trial_serial,
            last_trial_ts=excluded.last_trial_ts,
            is_open=excluded.is_open,
            is_open_delayed=excluded.is_open_delayed,
            is_close=excluded.is_close,
            is_close_delayed=excluded.is_close_delayed,
            raw_json=excluded.raw_json,
            synced_at=excluded.synced_at
        """,
        [
            {
                "stock_id": r.stock_id,
                "trade_date": r.trade_date,
                "poll_ts": r.poll_ts,
                "bid_prices": json.dumps(r.bid_prices, ensure_ascii=False)
                if r.bid_prices is not None
                else None,
                "bid_sizes": json.dumps(r.bid_sizes, ensure_ascii=False)
                if r.bid_sizes is not None
                else None,
                "ask_prices": json.dumps(r.ask_prices, ensure_ascii=False)
                if r.ask_prices is not None
                else None,
                "ask_sizes": json.dumps(r.ask_sizes, ensure_ascii=False)
                if r.ask_sizes is not None
                else None,
                "last_trade_price": r.last_trade_price,
                "last_trade_size": r.last_trade_size,
                "last_trade_serial": r.last_trade_serial,
                "last_trade_ts": r.last_trade_ts,
                "last_trial_price": r.last_trial_price,
                "last_trial_size": r.last_trial_size,
                "last_trial_serial": r.last_trial_serial,
                "last_trial_ts": r.last_trial_ts,
                "is_open": _bool_to_int(r.is_open),
                "is_open_delayed": _bool_to_int(r.is_open_delayed),
                "is_close": _bool_to_int(r.is_close),
                "is_close_delayed": _bool_to_int(r.is_close_delayed),
                "raw_json": r.raw_json,
                "source": r.source,
                "synced_at": r.synced_at,
            }
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)
