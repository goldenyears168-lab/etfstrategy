"""Neutral price-panel loader — DB → wide (trade_date × stock_id) DataFrame.

Extracted from ``research.backtest.finpilot_local_backtest`` so L2/L4 domain and
strategy modules can load close/open/volume panels WITHOUT importing the research
backtest layer (see docs/src-map.md import rule 3). ``finpilot_local_backtest``
re-exports ``load_price_panels`` from here for backward compatibility, so the
intra-research importers keep working unchanged.
"""

from __future__ import annotations

import sqlite3

import pandas as pd


def _wide_from_long(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Long → wide panel. pandas pivot mis-labels trade_date index on this dataset."""
    mat: dict[str, dict[str, float]] = {}
    for sid, td, val in zip(df["stock_id"], df["trade_date"], df[col], strict=True):
        mat.setdefault(td, {})[sid] = float(val)
    dates = sorted(mat)
    stocks = sorted({s for day in mat.values() for s in day})
    return pd.DataFrame(
        {s: [mat[d].get(s, float("nan")) for d in dates] for s in stocks},
        index=pd.Index(dates, name="trade_date"),
    )


def load_price_panels(conn: sqlite3.Connection) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, open, close, volume, source
        FROM stock_daily_bars
        ORDER BY trade_date, stock_id,
            CASE source WHEN 'finmind' THEN 0 WHEN 'yfinance' THEN 1 ELSE 2 END
        """
    ).fetchall()
    if not rows:
        raise RuntimeError("stock_daily_bars 無資料，請先跑 daily_sync / stock market sync")
    df = pd.DataFrame(rows, columns=["stock_id", "trade_date", "open", "close", "volume", "source"])
    df = df.drop_duplicates(subset=["stock_id", "trade_date"], keep="first")
    close = _wide_from_long(df, "close").astype(float)
    opn = _wide_from_long(df, "open").astype(float)
    vol = _wide_from_long(df, "volume").astype(float)
    return close, opn, vol
