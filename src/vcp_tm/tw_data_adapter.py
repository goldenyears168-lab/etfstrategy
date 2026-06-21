"""TW market data adapter: stocks.db → OHLCV panels for VCP-TM."""

from __future__ import annotations

import sqlite3

import pandas as pd

from holdings_research import TW_SPOT_CODE
from stock_context import load_daily_bars, load_tej_daily_bars
from vcp_nse_port.bars import rows_to_ohlcv_df
from vcp_tm.price_adapter import df_to_mrf_prices

DEFAULT_BAR_LIMIT = 280


def load_stock_panel(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    limit: int = DEFAULT_BAR_LIMIT,
) -> pd.DataFrame:
    rows = load_daily_bars(conn, stock_id, limit=limit)
    return rows_to_ohlcv_df(rows)


def load_benchmark_panel(
    conn: sqlite3.Connection,
    *,
    code: str = TW_SPOT_CODE,
    limit: int = DEFAULT_BAR_LIMIT,
) -> pd.DataFrame:
    rows = load_tej_daily_bars(conn, code, limit=limit)
    return rows_to_ohlcv_df(rows)


def load_stock_mrf(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    limit: int = DEFAULT_BAR_LIMIT,
) -> list[dict]:
    return df_to_mrf_prices(load_stock_panel(conn, stock_id, limit=limit))


def load_benchmark_mrf(
    conn: sqlite3.Connection,
    *,
    code: str = TW_SPOT_CODE,
    limit: int = DEFAULT_BAR_LIMIT,
) -> list[dict]:
    return df_to_mrf_prices(load_benchmark_panel(conn, code=code, limit=limit))
