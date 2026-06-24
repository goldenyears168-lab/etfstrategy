"""1 分 / 小時 K SQLite cache（rrg-lens-score-swap 回測 · FinMind / Yahoo）。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from stock_db.util import utc_now_iso


@dataclass(frozen=True)
class KbarBar:
    minute: str
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None


def upsert_stock_kbar_1m(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_kbar_1m (
            stock_id, trade_date, minute, open, high, low, close, volume, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :minute, :open, :high, :low, :close, :volume, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, minute, source) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def kbar_day_coverage(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    source: str = "finmind",
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM stock_kbar_1m
        WHERE stock_id = ? AND trade_date = ? AND source = ?
        """,
        (stock_id, trade_date, source),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


KBAR_SOURCE_PRIORITY = ("finmind", "yahoo")
MIN_BARS_PER_DAY = 4


def kbar_day_has_data(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM stock_kbar_1m
        WHERE stock_id = ? AND trade_date = ? AND source IN ('finmind', 'yahoo')
        """,
        (stock_id, trade_date),
    ).fetchone()
    return int(row[0] or 0) >= MIN_BARS_PER_DAY if row else False


def _row_to_kbar_bar(row: sqlite3.Row | dict[str, Any]) -> KbarBar | None:
    minute = str(row["minute"] if isinstance(row, sqlite3.Row) else row.get("minute") or "")
    close = row["close"] if isinstance(row, sqlite3.Row) else row.get("close")
    if not minute or close is None:
        return None
    try:
        px = float(close)
        if px <= 0:
            return None
    except (TypeError, ValueError):
        return None
    o = _opt_float(row["open"] if isinstance(row, sqlite3.Row) else row.get("open")) or px
    h = _opt_float(row["high"] if isinstance(row, sqlite3.Row) else row.get("high")) or px
    lo = _opt_float(row["low"] if isinstance(row, sqlite3.Row) else row.get("low")) or px
    vol = _opt_int(row["volume"] if isinstance(row, sqlite3.Row) else row.get("volume"))
    return KbarBar(minute=minute, open=o, high=h, low=lo, close=px, volume=vol)


def load_kbar_day_bars(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    source: str | None = None,
) -> tuple[KbarBar, ...]:
    """回傳升序 OHLCV 1 分 K；source=None 時 FinMind 優先、Yahoo 補洞。"""
    if source:
        sources = (source,)
    else:
        sources = KBAR_SOURCE_PRIORITY
    merged: dict[str, KbarBar] = {}
    for src in sources:
        rows = conn.execute(
            """
            SELECT minute, open, high, low, close, volume FROM stock_kbar_1m
            WHERE stock_id = ? AND trade_date = ? AND source = ?
            ORDER BY minute
            """,
            (stock_id, trade_date, src),
        ).fetchall()
        for row in rows:
            bar = _row_to_kbar_bar(row)
            if bar and bar.minute not in merged:
                merged[bar.minute] = bar
    return tuple(merged[k] for k in sorted(merged))


def kbar_bars_from_finmind_rows(
    finmind_rows: list[dict[str, Any]],
) -> tuple[KbarBar, ...]:
    out: list[KbarBar] = []
    for r in finmind_rows:
        bar = _row_to_kbar_bar(
            {
                "minute": r.get("minute") or r.get("time"),
                "open": r.get("open"),
                "high": r.get("max") or r.get("high"),
                "low": r.get("min") or r.get("low"),
                "close": r.get("close"),
                "volume": r.get("volume"),
            }
        )
        if bar:
            out.append(bar)
    out.sort(key=lambda b: b.minute)
    return tuple(out)


def load_kbar_day_closes(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    *,
    source: str | None = None,
) -> tuple[tuple[str, float], ...]:
    """回傳 (minute, close) 升序序列；source=None 時 FinMind 優先、Yahoo 補洞。"""
    if source:
        sources = (source,)
    else:
        sources = KBAR_SOURCE_PRIORITY
    merged: dict[str, float] = {}
    for src in sources:
        rows = conn.execute(
            """
            SELECT minute, close FROM stock_kbar_1m
            WHERE stock_id = ? AND trade_date = ? AND source = ?
            ORDER BY minute
            """,
            (stock_id, trade_date, src),
        ).fetchall()
        for row in rows:
            minute = str(row["minute"] or "")
            px = row["close"]
            if not minute or px is None or minute in merged:
                continue
            try:
                f = float(px)
                if f > 0:
                    merged[minute] = f
            except (TypeError, ValueError):
                continue
    return tuple(sorted(merged.items()))


def price_at_or_before_minute(
    bars: tuple[tuple[str, float], ...],
    hhmm: str,
) -> float | None:
    if not bars:
        return None
    target = hhmm if len(hhmm) > 5 else f"{hhmm}:00"
    last: float | None = None
    for minute, px in bars:
        if minute <= target:
            last = px
        else:
            break
    return last


def finmind_kbar_rows_to_db(
    stock_id: str,
    finmind_rows: list[dict[str, Any]],
    *,
    source: str = "finmind",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in finmind_rows:
        minute = str(r.get("minute") or r.get("time") or "").strip()
        trade_date = str(r.get("date") or r.get("trade_date") or "")[:10]
        close = r.get("close")
        if not minute or not trade_date or close is None:
            continue
        try:
            px = float(close)
            if px <= 0:
                continue
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "minute": minute,
                "open": _opt_float(r.get("open")),
                "high": _opt_float(r.get("max") or r.get("high")),
                "low": _opt_float(r.get("min") or r.get("low")),
                "close": px,
                "volume": _opt_int(r.get("volume")),
                "source": source,
            }
        )
    return out


def _opt_float(val: object) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _opt_int(val: object) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
