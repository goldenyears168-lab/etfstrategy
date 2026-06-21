"""TW OHLCV fetch · stock_daily_bars + daily_bars (IX0001) cache in stocks.db."""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import date, timedelta

import pandas as pd

from finmind_client import fetch_finmind, finmind_token
from query_stock_prices import (
    YAHOO_BENCHMARKS,
    fetch_finmind_daily,
    fetch_yahoo_index_bars,
)
from stock_db import upsert_daily_bars, upsert_stock_daily_bars
from vcp_nse_port.bars import rows_to_ohlcv_df

YFINANCE_SOURCE = "yfinance"


def yfinance_tw_rows(stock_id: str, start: date, end: date) -> list[dict]:
    import yfinance as yf

    symbol = f"{stock_id}.TW"
    df = yf.download(
        symbol,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        progress=False,
        auto_adjust=True,
        threads=False,
    )
    if df.empty:
        return []
    if hasattr(df.columns, "get_level_values"):
        df.columns = df.columns.get_level_values(0)
    rows: list[dict] = []
    for idx, row in df.iterrows():
        close = float(row.get("Close", 0))
        if close <= 0:
            continue
        rows.append(
            {
                "date": str(idx.date()),
                "open": float(row.get("Open", close)),
                "high": float(row.get("High", close)),
                "low": float(row.get("Low", close)),
                "close": close,
                "volume": float(row.get("Volume", 0) or 0),
            }
        )
    return rows


def _db_stock_rows(
    conn: sqlite3.Connection,
    stock_id: str,
    start: date,
    end: date,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT trade_date, open, high, low, close, volume, source
        FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind'
          AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC
        """,
        (stock_id, start.isoformat(), end.isoformat()),
    ).fetchall()


def _db_benchmark_rows(
    conn: sqlite3.Connection,
    code: str,
    start: date,
    end: date,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT date AS trade_date, open, high, low, close, volume, source
        FROM daily_bars
        WHERE code = ? AND date >= ? AND date <= ?
        ORDER BY date ASC
        """,
        (code, start.isoformat(), end.isoformat()),
    ).fetchall()


def finmind_stock_rows(stock_id: str, start: date, end: date) -> list[dict]:
    raw = fetch_finmind("TaiwanStockPrice", stock_id, start, end)
    rows: list[dict] = []
    for item in raw:
        td = str(item.get("date", ""))[:10]
        close = item.get("close")
        if not td or close is None:
            continue
        rows.append(
            {
                "date": td,
                "open": item.get("open") or close,
                "high": item.get("max") or close,
                "low": item.get("min") or close,
                "close": close,
                "volume": item.get("Trading_Volume") or 0,
            }
        )
    return rows


def _rows_for_stock_db(stock_id: str, raw_rows: list[dict], source: str) -> list[dict]:
    db_rows: list[dict] = []
    for r in raw_rows:
        trade_date = str(r.get("date") or r.get("trade_date") or "")[:10]
        close = r.get("close")
        if not trade_date or close is None:
            continue
        db_rows.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "open": r.get("open") or close,
                "high": r.get("high") or close,
                "low": r.get("low") or close,
                "close": close,
                "volume": r.get("volume") or 0,
                "source": source,
            }
        )
    return db_rows


def _rows_for_benchmark_db(code: str, raw_rows: list[dict], source: str) -> list[dict]:
    db_rows: list[dict] = []
    for r in raw_rows:
        trade_date = str(r.get("date") or r.get("trade_date") or "")[:10]
        close = r.get("close")
        if not trade_date or close is None:
            continue
        db_rows.append(
            {
                "code": code,
                "date": trade_date,
                "open": r.get("open") or close,
                "high": r.get("high") or close,
                "low": r.get("low") or close,
                "close": close,
                "volume": r.get("volume") or 0,
                "spread": None,
                "source": source,
            }
        )
    return db_rows


def db_rows_to_df(rows: list[sqlite3.Row]) -> pd.DataFrame:
    by_date: dict[str, dict] = {}
    for row in rows:
        d = dict(row)
        key = str(d["trade_date"])
        if key not in by_date or d.get("source") == "finmind":
            by_date[key] = {
                "date": key,
                "open": d["open"],
                "high": d["high"],
                "low": d["low"],
                "close": d["close"],
                "volume": d.get("volume") or 0,
            }
    return rows_to_ohlcv_df(list(by_date.values()))


def fetch_tw_stock_rows(
    stock_id: str,
    start: date,
    end: date,
    *,
    prefer_finmind: bool,
) -> tuple[list[dict], str]:
    if prefer_finmind and finmind_token():
        try:
            rows = finmind_stock_rows(stock_id, start, end)
            if rows:
                return rows, "finmind"
        except Exception as exc:
            print(f"  FinMind {stock_id} fallback yfinance: {exc}", file=sys.stderr)
    rows = yfinance_tw_rows(stock_id, start, end)
    return rows, YFINANCE_SOURCE if rows else "none"


def fetch_tw_benchmark_rows(
    code: str,
    start: date,
    end: date,
    *,
    prefer_finmind: bool,
) -> tuple[list[dict], str]:
    if prefer_finmind and finmind_token():
        try:
            rows = fetch_finmind_daily(code, start, end)
            if rows:
                return [
                    {
                        "date": r["date"],
                        "open": r["open"],
                        "high": r["high"],
                        "low": r["low"],
                        "close": r["close"],
                        "volume": r["volume"],
                    }
                    for r in rows
                ], "finmind"
        except Exception as exc:
            print(f"  FinMind benchmark {code}: {exc}", file=sys.stderr)

    yahoo_map = {code: YAHOO_BENCHMARKS[code]} if code in YAHOO_BENCHMARKS else {}
    if yahoo_map:
        yahoo_rows = fetch_yahoo_index_bars(yahoo_map, start, end)
        if yahoo_rows:
            return [
                {
                    "date": r["date"],
                    "open": r.get("open") or r["close"],
                    "high": r.get("high") or r["close"],
                    "low": r.get("low") or r["close"],
                    "close": r["close"],
                    "volume": r.get("volume") or 0,
                }
                for r in yahoo_rows
            ], "yahoo"
    return [], "none"


def sync_tw_ticker(
    conn: sqlite3.Connection,
    stock_id: str,
    start: date,
    end: date,
    *,
    prefer_finmind: bool = True,
) -> tuple[int, str]:
    raw, source = fetch_tw_stock_rows(stock_id, start, end, prefer_finmind=prefer_finmind)
    rows = _rows_for_stock_db(stock_id, raw, source)
    n = upsert_stock_daily_bars(conn, rows)
    return n, source


def sync_tw_benchmark(
    conn: sqlite3.Connection,
    code: str,
    start: date,
    end: date,
    *,
    prefer_finmind: bool = True,
) -> tuple[int, str]:
    raw, source = fetch_tw_benchmark_rows(code, start, end, prefer_finmind=prefer_finmind)
    rows = _rows_for_benchmark_db(code, raw, source)
    n = upsert_daily_bars(conn, rows)
    return n, source


def _needs_fetch(
    df: pd.DataFrame | None,
    start: date,
    *,
    min_bars: int = 200,
) -> bool:
    if df is None or len(df) < min_bars:
        return True
    first = df["date"].iloc[0].date()
    return first > start + timedelta(days=30)


def load_tw_panel(
    tickers: tuple[str, ...],
    benchmark: str,
    start: date,
    end: date,
    *,
    conn: sqlite3.Connection | None = None,
    use_db: bool = True,
    prefer_finmind: bool = True,
    pause_sec: float = 0.35,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, str]:
    panels: dict[str, pd.DataFrame] = {}
    source = "finmind"
    start_s, end_s = start.isoformat(), end.isoformat()

    for ticker in tickers:
        df: pd.DataFrame | None = None
        if conn is not None and use_db:
            cached = _db_stock_rows(conn, ticker, start, end)
            if cached:
                df = db_rows_to_df(cached)
                source = str(dict(cached[0]).get("source") or source)

        if _needs_fetch(df, start):
            raw, src = fetch_tw_stock_rows(ticker, start, end, prefer_finmind=prefer_finmind)
            if raw:
                source = src
                if conn is not None and use_db and src != "none":
                    upsert_stock_daily_bars(conn, _rows_for_stock_db(ticker, raw, src))
                df = rows_to_ohlcv_df(raw)

        if (df is None or len(df) < 200) and conn is not None and use_db:
            cached_all = conn.execute(
                """
                SELECT trade_date, open, high, low, close, volume, source
                FROM stock_daily_bars
                WHERE stock_id = ? AND source = 'finmind'
                ORDER BY trade_date ASC
                """,
                (ticker,),
            ).fetchall()
            if cached_all and len(cached_all) >= 200:
                df = db_rows_to_df(cached_all)
                source = str(dict(cached_all[0]).get("source") or source)

        if df is not None and len(df) >= 200:
            panels[ticker] = df
        else:
            n = 0 if df is None else len(df)
            print(f"  SKIP {ticker}: bars={n} (<200)", file=sys.stderr)
        if prefer_finmind and finmind_token():
            time.sleep(pause_sec)
        else:
            time.sleep(0.08)

    bench_df: pd.DataFrame | None = None
    if conn is not None and use_db:
        cached_b = _db_benchmark_rows(conn, benchmark, start, end)
        if cached_b:
            bench_df = db_rows_to_df(cached_b)
            source = str(dict(cached_b[0]).get("source") or source)

    if _needs_fetch(bench_df, start):
        raw_b, src_b = fetch_tw_benchmark_rows(
            benchmark, start, end, prefer_finmind=prefer_finmind
        )
        if raw_b:
            if conn is not None and use_db:
                upsert_daily_bars(conn, _rows_for_benchmark_db(benchmark, raw_b, src_b))
            bench_df = rows_to_ohlcv_df(raw_b)
            source = src_b

    if bench_df is None:
        bench_df = pd.DataFrame()

    return panels, bench_df, source
