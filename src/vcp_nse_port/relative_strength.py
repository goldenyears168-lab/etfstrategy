"""Minervini-weighted relative strength vs benchmark (no yfinance)."""

from __future__ import annotations

import pandas as pd


def calculate_relative_strength(
    stock_df: pd.DataFrame,
    benchmark_df: pd.DataFrame | None = None,
) -> dict:
    close = stock_df["Close"]
    current = float(close.iloc[-1])

    periods = {
        "3m": min(63, len(close) - 1),
        "6m": min(126, len(close) - 1),
        "9m": min(189, len(close) - 1),
        "12m": min(252, len(close) - 1),
    }

    stock_returns: dict[str, float] = {}
    for label, days in periods.items():
        if days > 0:
            past = float(close.iloc[-days - 1])
            stock_returns[label] = (current - past) / past * 100 if past > 0 else 0.0
        else:
            stock_returns[label] = 0.0

    bench_returns = {"3m": 0.0, "6m": 0.0, "9m": 0.0, "12m": 0.0}
    if benchmark_df is not None and len(benchmark_df) > 0:
        bench_close = benchmark_df["Close"]
        bench_current = float(bench_close.iloc[-1])
        for label, days in periods.items():
            bdays = min(days, len(bench_close) - 1)
            if bdays > 0:
                past = float(bench_close.iloc[-bdays - 1])
                bench_returns[label] = (
                    (bench_current - past) / past * 100 if past > 0 else 0.0
                )

    excess = {k: stock_returns[k] - bench_returns[k] for k in stock_returns}
    rs_value = (
        0.40 * excess["3m"]
        + 0.20 * excess["6m"]
        + 0.20 * excess["9m"]
        + 0.20 * excess["12m"]
    )

    return {
        "rs_value": round(rs_value, 2),
        "score": round(_score_rs(rs_value), 1),
        "stock_returns": {k: round(v, 2) for k, v in stock_returns.items()},
        "benchmark_returns": {k: round(v, 2) for k, v in bench_returns.items()},
        "excess_returns": {k: round(v, 2) for k, v in excess.items()},
    }


def _score_rs(rs: float) -> float:
    if rs > 50:
        return 95.0
    if rs > 30:
        return 80.0
    if rs > 15:
        return 65.0
    if rs > 5:
        return 50.0
    if rs > 0:
        return 35.0
    return 15.0
