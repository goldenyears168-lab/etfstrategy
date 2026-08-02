"""TW market benchmark helpers (IX0001 weighted index)."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pandas as pd


def load_benchmark_close(conn: sqlite3.Connection, *, code: str = "IX0001") -> pd.Series:
    """Per-date priority: tej → finmind → yahoo → other (backfill 2015–2019 FinMind TAIEX)."""
    rows = conn.execute(
        """
        SELECT date AS trade_date, close
        FROM daily_bars
        WHERE code = ?
        ORDER BY date,
            CASE source
                WHEN 'tej' THEN 0
                WHEN 'finmind' THEN 1
                WHEN 'yahoo' THEN 2
                ELSE 3
            END
        """,
        (code,),
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["trade_date", "close"])
    df = df.drop_duplicates(subset=["trade_date"], keep="first")
    return df.set_index("trade_date")["close"].astype(float)


def latest_trading_date(
    conn: sqlite3.Connection,
    *,
    on_or_before: str | date | None = None,
    code: str = "IX0001",
) -> str | None:
    """最新交易日（交易日曆，非日曆日）。

    錨定 `stock_daily_bars.trade_date`（watchlist 聯集日線的交易日曆，收盤 ingest 一到
    即更新），不再綁易腐化的 IX0001/tej 指數 feed —— 2026-08 健檢發現 tej 錨點曾落後
    16 交易日，令收盤主線靜默套用 3 週前日期。`code` 參數保留供既有呼叫相容，錨點來源
    已不使用它。
    """
    ceiling = None
    if on_or_before is not None:
        ceiling = (
            on_or_before.isoformat()
            if isinstance(on_or_before, date)
            else str(on_or_before)
        )
    if ceiling:
        row = conn.execute(
            "SELECT MAX(trade_date) AS d FROM stock_daily_bars WHERE trade_date <= ?",
            (ceiling,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(trade_date) AS d FROM stock_daily_bars"
        ).fetchone()
    if not row or not row["d"]:
        return None
    return str(row["d"])


def resolve_brief_trade_date(
    conn: sqlite3.Connection,
    candidate: date,
    *,
    code: str = "IX0001",
) -> date:
    """將日曆日對齊為 ≤ candidate 的最近交易日（週末／假日 → 前一交易日）。"""
    resolved = latest_trading_date(conn, on_or_before=candidate, code=code)
    if not resolved:
        return candidate
    return date.fromisoformat(resolved)


def is_trading_date(
    conn: sqlite3.Connection,
    day: date,
    *,
    code: str = "IX0001",
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM stock_daily_bars WHERE trade_date = ? LIMIT 1",
        (day.isoformat(),),
    ).fetchone()
    return row is not None


def is_trading_session_day(
    conn: sqlite3.Connection,
    day: date,
    *,
    code: str = "IX0001",
    max_gap_days: int = 4,
) -> bool:
    """TEJ 日線已落庫，或週一～五盤中（上一 TEJ 交易日距今 ≤ max_gap_days 曆日）。"""
    if is_trading_date(conn, day, code=code):
        return True
    if day.weekday() >= 5:
        return False
    prev = latest_trading_date(conn, on_or_before=day - timedelta(days=1), code=code)
    if not prev:
        return False
    gap = (day - date.fromisoformat(prev)).days
    return 1 <= gap <= max_gap_days


def previous_trading_date(
    conn: sqlite3.Connection,
    trade_date: str | date,
    *,
    code: str = "IX0001",
) -> str | None:
    """Strictly before trade_date 的最近交易日。"""
    if isinstance(trade_date, date):
        ceiling = trade_date - timedelta(days=1)
    else:
        ceiling = date.fromisoformat(str(trade_date)) - timedelta(days=1)
    return latest_trading_date(conn, on_or_before=ceiling, code=code)


def list_trading_dates(
    conn: sqlite3.Connection,
    *,
    end: str,
    limit: int,
    code: str = "IX0001",
) -> list[str]:
    """含 end 在內、往回 limit 個 TEJ 交易日（升序）。"""
    rows = conn.execute(
        """
        SELECT date FROM daily_bars
        WHERE code = ? AND source = 'tej' AND date <= ?
        ORDER BY date DESC
        LIMIT ?
        """,
        (code, end, limit),
    ).fetchall()
    return [str(r[0]) for r in reversed(rows)]
