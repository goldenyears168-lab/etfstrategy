"""Convert pandas OHLCV (ascending) to tradermonty most-recent-first price dicts."""

from __future__ import annotations

import pandas as pd


def df_to_mrf_prices(df: pd.DataFrame) -> list[dict]:
    """Ascending DataFrame → list[dict] most-recent-first with lowercase OHLCV keys."""
    if df is None or df.empty:
        return []

    work = df.copy()
    if "date" in work.columns:
        work = work.sort_values("date")
    records: list[dict] = []
    for _, row in work.iterrows():
        d = row.get("date")
        date_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
        close = float(row["Close"])
        records.append(
            {
                "date": date_str,
                "open": float(row.get("Open", close)),
                "high": float(row.get("High", close)),
                "low": float(row.get("Low", close)),
                "close": close,
                "volume": float(row.get("Volume", 0) or 0),
            }
        )
    return list(reversed(records))


def quote_from_mrf(historical_prices: list[dict]) -> dict:
    """Build quote_data for trend template from price history."""
    if not historical_prices:
        return {"price": 0, "yearHigh": 0, "yearLow": 0}

    price = float(historical_prices[0].get("close", 0))
    window = historical_prices[: min(252, len(historical_prices))]
    highs = [float(d.get("high", d.get("close", 0))) for d in window]
    lows = [float(d.get("low", d.get("close", 0))) for d in window]
    return {
        "price": price,
        "yearHigh": max(highs) if highs else price,
        "yearLow": min(lows) if lows else price,
    }
