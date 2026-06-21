"""US OHLCV fetch · optional us_daily_bars cache in stocks.db."""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

from finmind_client import fetch_finmind, finmind_token
from stock_db import connect, upsert_us_daily_bars, load_us_daily_bars
from vcp_nse_port.bars import rows_to_ohlcv_df


def yfinance_us_rows(ticker: str, start: date, end: date) -> list[dict]:
    import yfinance as yf

    df = yf.download(ticker, start=start.isoformat(), end=end.isoformat(), progress=False)
    if df.empty:
        return []
    if hasattr(df.columns, "get_level_values"):
        df.columns = df.columns.get_level_values(0)
    rows: list[dict] = []
    for idx, row in df.iterrows():
        rows.append(
            {
                "date": str(idx.date()),
                "open": float(row.get("Open", row.get("Close", 0))),
                "high": float(row.get("High", row.get("Close", 0))),
                "low": float(row.get("Low", row.get("Close", 0))),
                "close": float(row.get("Close", 0)),
                "volume": float(row.get("Volume", 0) or 0),
            }
        )
    return rows


def finmind_us_rows(ticker: str, start: date, end: date) -> list[dict]:
    rows = fetch_finmind("USStockPrice", ticker, start, end)
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "date": r.get("date"),
                "open": r.get("Open") or r.get("open"),
                "high": r.get("High") or r.get("high"),
                "low": r.get("Low") or r.get("low"),
                "close": r.get("Close") or r.get("close") or r.get("Adj_Close"),
                "volume": r.get("Volume") or r.get("volume") or 0,
            }
        )
    return out


def fetch_us_rows(
    ticker: str,
    start: date,
    end: date,
    *,
    prefer_finmind: bool,
) -> tuple[list[dict], str]:
    if prefer_finmind and finmind_token():
        try:
            return finmind_us_rows(ticker, start, end), "finmind"
        except Exception as exc:
            print(f"  FinMind {ticker} fallback yfinance: {exc}", file=sys.stderr)
    return yfinance_us_rows(ticker, start, end), "yfinance"


def _rows_for_db(ticker: str, raw_rows: list[dict], source: str) -> list[dict]:
    db_rows: list[dict] = []
    for r in raw_rows:
        trade_date = str(r.get("date") or r.get("trade_date") or "")[:10]
        close = r.get("close") or r.get("Close")
        if not trade_date or close is None:
            continue
        db_rows.append(
            {
                "ticker": ticker.upper(),
                "trade_date": trade_date,
                "open": r.get("open") or r.get("Open") or close,
                "high": r.get("high") or r.get("High") or close,
                "low": r.get("low") or r.get("Low") or close,
                "close": close,
                "volume": r.get("volume") or r.get("Volume") or 0,
                "source": source,
            }
        )
    return db_rows


def sync_us_ticker(
    conn: sqlite3.Connection,
    ticker: str,
    start: date,
    end: date,
    *,
    prefer_finmind: bool = True,
) -> tuple[int, str]:
    raw, source = fetch_us_rows(ticker, start, end, prefer_finmind=prefer_finmind)
    rows = _rows_for_db(ticker, raw, source)
    n = upsert_us_daily_bars(conn, rows)
    return n, source


def db_rows_to_df(rows: list[sqlite3.Row]) -> pd.DataFrame:
    """Prefer finmind over yfinance when both exist for same date."""
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


def load_us_panel(
    tickers: tuple[str, ...],
    benchmark: str,
    start: date,
    end: date,
    *,
    conn: sqlite3.Connection | None = None,
    use_db: bool = True,
    prefer_finmind: bool = True,
    pause_sec: float = 0.08,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, str]:
    panels: dict[str, pd.DataFrame] = {}
    source = "yfinance"
    start_s, end_s = start.isoformat(), end.isoformat()

    for ticker in tickers:
        df: pd.DataFrame | None = None
        if conn is not None and use_db:
            cached = load_us_daily_bars(conn, ticker, start=start_s, end=end_s)
            if cached:
                df = db_rows_to_df(cached)
                source = str(dict(cached[0]).get("source") or source)

        if df is None or len(df) < 200:
            raw, src = fetch_us_rows(ticker, start, end, prefer_finmind=prefer_finmind)
            source = src
            if conn is not None and use_db and raw:
                upsert_us_daily_bars(conn, _rows_for_db(ticker, raw, src))
            df = rows_to_ohlcv_df(raw)

        if len(df) >= 200:
            panels[ticker.upper()] = df
        else:
            print(f"  SKIP {ticker}: bars={len(df)} (<200)", file=sys.stderr)
        time.sleep(pause_sec if prefer_finmind and finmind_token() else 0.03)

    bench_df: pd.DataFrame | None = None
    if conn is not None and use_db:
        cached_b = load_us_daily_bars(conn, benchmark, start=start_s, end=end_s)
        if cached_b:
            bench_df = db_rows_to_df(cached_b)
    if bench_df is None or bench_df.empty:
        raw_b, src_b = fetch_us_rows(benchmark, start, end, prefer_finmind=prefer_finmind)
        if conn is not None and use_db and raw_b:
            upsert_us_daily_bars(conn, _rows_for_db(benchmark, raw_b, src_b))
        bench_df = rows_to_ohlcv_df(raw_b)
        source = src_b if raw_b else source

    return panels, bench_df, source
