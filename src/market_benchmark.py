"""TW market benchmark helpers (IX0001 weighted index)."""

from __future__ import annotations

import sqlite3

import pandas as pd


def load_benchmark_close(conn: sqlite3.Connection, *, code: str = "IX0001") -> pd.Series:
    rows = conn.execute(
        """
        SELECT date AS trade_date, close
        FROM daily_bars
        WHERE code = ? AND source = 'tej'
        ORDER BY date
        """,
        (code,),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            """
            SELECT date AS trade_date, close
            FROM daily_bars
            WHERE code = ?
            ORDER BY date
            """,
            (code,),
        ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["trade_date", "close"])
    return df.set_index("trade_date")["close"].astype(float)
