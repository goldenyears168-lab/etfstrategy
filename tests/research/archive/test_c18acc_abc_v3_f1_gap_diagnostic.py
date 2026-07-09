"""Tests · gap diagnostic offline builder + 5m kbar helper."""

from __future__ import annotations

from research.backtest.archive.c18acc_abc_v3_f1_gap_diagnostic import (
    build_gap_diagnostic,
    render_gap_diagnostic_md,
)
from stock_db.kbar import aggregate_closes_to_bars, price_at_or_before_minute


def test_aggregate_closes_to_bars_5m():
    closes = (
        ("09:00", 10.0),
        ("09:01", 10.1),
        ("09:04", 10.2),
        ("09:05", 10.3),
        ("09:09", 10.4),
    )
    bars = aggregate_closes_to_bars(closes, bar_minutes=5)
    assert bars == (("09:00", 10.2), ("09:05", 10.4))
    assert price_at_or_before_minute(bars, "09:07") == 10.4
    assert price_at_or_before_minute(bars, "09:04") == 10.2


def test_gap_diagnostic_from_minimal_payload():
    payload = {
        "schema": "c18acc_abc_v3_f1_comparison-v1",
        "generated_at": "2026-07-08",
        "date_start": "2024-01-02",
        "date_end": "2024-01-10",
        "n_trade_dates": 3,
        "strategies": {
            "rrg-mono-swap-accel": {
                "n_slots": 3,
                "slot_cap_ntd": 33333.0,
                "portfolio": {
                    "total_return_pct": 10.0,
                    "mean_daily_return_pct": 0.2,
                    "ann_mean_return_pct": 50.0,
                    "avg_utilization_pct": 90.0,
                    "n_trades": 1,
                    "avg_hold_days": 9.0,
                    "daily_nav": [
                        {"date": "2024-01-02", "equity_ntd": 100000, "in_market_flag": 1, "cash_pct": 10},
                        {"date": "2024-01-03", "equity_ntd": 101000, "in_market_flag": 1, "cash_pct": 10},
                        {"date": "2024-01-04", "equity_ntd": 110000, "in_market_flag": 1, "cash_pct": 5},
                    ],
                },
            },
            "abc-v3-f1-pullback": {
                "n_slots": 5,
                "slot_cap_ntd": 20000.0,
                "hold_days": 3,
                "n_candidates": 2,
                "portfolio": {
                    "total_return_pct": 2.0,
                    "mean_daily_return_pct": 0.05,
                    "ann_mean_return_pct": 12.0,
                    "avg_utilization_pct": 40.0,
                    "n_trades": 1,
                    "avg_hold_days": 3.0,
                    "daily_nav": [
                        {"date": "2024-01-02", "equity_ntd": 100000, "in_market_flag": 0, "cash_pct": 100},
                        {"date": "2024-01-03", "equity_ntd": 100000, "in_market_flag": 1, "cash_pct": 80},
                        {"date": "2024-01-04", "equity_ntd": 102000, "in_market_flag": 1, "cash_pct": 60},
                    ],
                },
            },
        },
        "comparison": {"abc_f1_signal_capture_pct": 50.0},
        "event_level": {
            "champion_pipeline": {"n": 170, "mean_ret_3d_net_pct": 2.845},
            "abc_f1": {
                "all_candidates": {"n": 2, "mean_ret_net_pct": 1.0},
                "executed_topk": {"n": 1, "mean_ret_net_pct": 1.5},
            },
            "c18acc": {"n": 1, "mean_ret_net_pct": 6.0},
        },
        "c18acc_trade_ledger": [
            {
                "entry_date": "2024-01-02",
                "exit_date": "2024-01-04",
                "return_pct": 6.0,
                "stock_id": "2330",
            }
        ],
        "abc_f1_trade_ledger": [
            {
                "entry_date": "2024-01-03",
                "exit_date": "2024-01-04",
                "net_pct": 1.5,
                "stock_id": "2454",
            }
        ],
    }
    diag = build_gap_diagnostic(payload)
    assert diag["schema"] == "c18acc_abc_v3_f1_gap_diagnostic-v1"
    assert diag["kbar_policy"]["pricing_preference"] == "5m_aggregated"
    assert "daily_filled_slots" not in (diag["occupancy"]["abc_f1"] or {})
    md = render_gap_diagnostic_md(diag)
    assert "空槽" in md
    assert "5m" in md
