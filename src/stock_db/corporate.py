"""Corporate actions (dividends, splits) from Yahoo Chart API."""
from __future__ import annotations

import sqlite3

from stock_db.util import utc_now_iso


def upsert_stock_corporate_actions(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_corporate_actions (
            symbol_key, ex_date, action_type, amount,
            split_numerator, split_denominator, split_ratio,
            source, synced_at
        ) VALUES (
            :symbol_key, :ex_date, :action_type, :amount,
            :split_numerator, :split_denominator, :split_ratio,
            :source, :synced_at
        )
        ON CONFLICT(symbol_key, ex_date, action_type, source) DO UPDATE SET
            amount=excluded.amount,
            split_numerator=excluded.split_numerator,
            split_denominator=excluded.split_denominator,
            split_ratio=excluded.split_ratio,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)
