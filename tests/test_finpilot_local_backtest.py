"""Tests for FinPilot local strategy selection."""

from __future__ import annotations

import sqlite3

import pandas as pd

from research.backtest.finpilot_local_backtest import (  # noqa: E402
    _revenue_yoy_at_date,
    _roe_at_date,
    load_financial_history,
    month_end_trading_dates,
    pit_fundamental_at,
    select_stocks,
)


def test_month_end_trading_dates():
    dates = ["2024-01-02", "2024-01-31", "2024-02-01", "2024-02-29"]
    assert month_end_trading_dates(dates) == ["2024-01-31", "2024-02-29"]


def test_s01_new_high_selects_top_by_close():
    idx = pd.date_range("2023-01-01", periods=260, freq="B")
    dates = [d.strftime("%Y-%m-%d") for d in idx]
    close = pd.DataFrame(
        {
            "2330": [100.0] * 259 + [110.0],
            "2317": [50.0] * 260,
        },
        index=dates,
    )
    vol = pd.DataFrame(4_000_000.0, index=dates, columns=close.columns)
    picks = select_stocks(
        "s01",
        signal_date=dates[-1],
        close=close,
        vol=vol,
        fund_snap={},
    )
    assert picks == ["2330"]


def test_pit_fundamental_uses_history_roe():
    fund = pd.DataFrame(columns=["stock_id", "as_of_date", "roe_latest_q", "revenue_yoy_pct"])
    fin = pd.DataFrame(
        [
            ("2330", "2024-03-31", "quarter", "net_income", 100.0),
            ("2330", "2024-03-31", "quarter", "equity", 500.0),
        ],
        columns=["stock_id", "period_date", "period_type", "metric", "value"],
    )
    snap = pit_fundamental_at(fund, fin, ["2330"], "2024-06-01")
    assert snap["2330"]["roe_latest_q"] == 20.0


def test_revenue_yoy_from_monthly_history():
    fin = pd.DataFrame(
        [
            ("2330", "2024-05-01", "month", "revenue", 100.0),
            ("2330", "2025-05-01", "month", "revenue", 120.0),
        ],
        columns=["stock_id", "period_date", "period_type", "metric", "value"],
    )
    # 5 月營收次月 10 日才公告 → 6/15 已可得，YoY=20%
    assert _revenue_yoy_at_date(fin, "2330", "2025-06-15") == 20.0


def test_revenue_yoy_no_lookahead_before_announce():
    """PIT：5 月營收 6/10 才公布，6/01 訊號日不得用 2025-05 → 無可比即回 None。"""
    fin = pd.DataFrame(
        [
            ("2330", "2024-05-01", "month", "revenue", 100.0),
            ("2330", "2025-05-01", "month", "revenue", 120.0),
        ],
        columns=["stock_id", "period_date", "period_type", "metric", "value"],
    )
    # 6/01 時 2025-05 尚未公告；可得的最新是 2024-05，但其 YoY 需 2023-05（無）→ None
    assert _revenue_yoy_at_date(fin, "2330", "2025-06-01") is None


def test_roe_no_lookahead_before_announce():
    """PIT：Q1（3/31）約 5/15 才公告，4/01 訊號日不得偷看 → None；5/20 才可得。"""
    fin = pd.DataFrame(
        [
            ("2330", "2024-03-31", "quarter", "net_income", 100.0),
            ("2330", "2024-03-31", "quarter", "equity", 500.0),
        ],
        columns=["stock_id", "period_date", "period_type", "metric", "value"],
    )
    assert _roe_at_date(fin, "2330", "2024-04-01") is None
    assert _roe_at_date(fin, "2330", "2024-05-20") == 20.0
