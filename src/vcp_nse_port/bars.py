"""OHLCV adapter: SQLite rows / FinMind dicts → pandas DataFrame."""

from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd


def rows_to_ohlcv_df(rows: list[sqlite3.Row | dict[str, Any]]) -> pd.DataFrame:
    """Normalize daily bars to ascending DataFrame with Close/High/Low/Volume."""
    if not rows:
        return pd.DataFrame()

    records: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        trade_date = d.get("trade_date") or d.get("date")
        close = d.get("close") or d.get("Close")
        if trade_date is None or close is None:
            continue
        records.append(
            {
                "date": str(trade_date)[:10],
                "Open": float(d.get("open") or d.get("Open") or close),
                "High": float(d.get("high") or d.get("High") or close),
                "Low": float(d.get("low") or d.get("Low") or close),
                "Close": float(close),
                "Volume": float(d.get("volume") or d.get("Volume") or 0),
            }
        )

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    df = df.reset_index(drop=True)
    return df
