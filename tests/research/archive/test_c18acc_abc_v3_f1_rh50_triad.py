"""Tests for C18acc / ABC f1 / RH50 triad helpers."""

from __future__ import annotations

from research.backtest.archive.c18acc_abc_v3_f1_rh50_triad import (
    build_layered_metrics,
    render_triad_md,
)


def test_build_layered_metrics_only_invested():
    nav = [
        {"date": "2024-01-02", "equity_ntd": 100_000, "in_market_flag": 0, "cash_pct": 100},
        {"date": "2024-01-03", "equity_ntd": 100_000, "in_market_flag": 1, "cash_pct": 40},
        {"date": "2024-01-04", "equity_ntd": 101_000, "in_market_flag": 1, "cash_pct": 40},
        {"date": "2024-01-05", "equity_ntd": 101_000, "in_market_flag": 0, "cash_pct": 100},
    ]
    pf = {
        "mean_daily_return_pct": 0.25,
        "ann_mean_return_pct": 63.0,
        "total_return_pct": 1.0,
        "avg_utilization_pct": 50.0,
    }
    layered = build_layered_metrics(nav, pf, tail_days=3)
    assert layered["full_nav"]["in_market_days"] == 2
    assert layered["only_invested"]["n_days"] == 2
    # day2→3: 0%, day3→4: +1%  (only days with prior+current both considered; invested on day of return?)
    # returns attach to current day; invested filter uses current day's flag
    assert layered["util_normalized"]["mean_daily_return_pct"] == 0.5
    assert layered["last_90d"]["n_days"] == 3


def test_render_triad_md_headers():
    empty_layer = {
        "full_nav": {"mean_daily_return_pct": 0.2, "avg_utilization_pct": 90, "n_days": 3, "in_market_days": 2, "in_market_pct": 66},
        "only_invested": {"mean_daily_return_pct": 0.3, "n_days": 2},
        "util_normalized": {"mean_daily_return_pct": 0.22},
        "last_90d": {
            "date_start": "2026-02-25",
            "date_end": "2026-07-07",
            "total_return_pct": 10,
            "mean_daily_return_pct": 0.1,
            "only_invested_mean_daily_pct": 0.15,
            "in_market_days": 80,
            "deployed_proxy_pct": 50,
        },
    }
    payload = {
        "generated_at": "2026-07-08",
        "total_runtime_s": 1.0,
        "date_start": "2024-01-02",
        "date_end": "2026-07-07",
        "n_trade_dates": 606,
        "kbar_policy": {"pricing_preference": "5m_aggregated", "rebuild_panel": False, "note": "x"},
        "comparison_contract": {"notional_ntd": 100000, "cost_model": {"enabled": True}},
        "rh50_gate": {
            "rule": "RH50",
            "pit": "T-1",
            "flagged_session_days": 300,
            "total_session_days": 606,
            "allowed_share_pct": 50.0,
        },
        "strategies": {
            "rrg-mono-swap-accel": {
                "portfolio": {"total_return_pct": 1, "mean_daily_return_pct": 0.2},
                "leg_stats": {"n": 1, "mean_ret_pct": 5, "win_rate_pct": 60},
                "layered": empty_layer,
            },
            "abc-v3-f1-pullback": {
                "portfolio": {"total_return_pct": 2, "mean_daily_return_pct": 0.1},
                "leg_stats": {"n": 2, "mean_ret_pct": 1, "win_rate_pct": 50},
                "layered": empty_layer,
            },
            "abc-v3-f1-rh50": {
                "portfolio": {"total_return_pct": 1.5, "mean_daily_return_pct": 0.08},
                "leg_stats": {"n": 1, "mean_ret_pct": 1.5, "win_rate_pct": 55},
                "layered": empty_layer,
            },
        },
        "comparison": {
            "winner_daily": "rrg-mono-swap-accel",
            "winner_only_invested": "abc-v3-f1-pullback",
            "winner_util_normalized": "abc-v3-f1-pullback",
        },
        "process_log": [{"ts": "t", "elapsed_s": 0, "phase": "init", "detail": "ok"}],
        "abc_f1_rh50_trade_ledger": [],
    }
    md = render_triad_md(payload)
    assert "三者比較" in md
    assert "分層對照" in md
    assert "only-invested" in md
    assert "util-normalized" in md
    assert "5m_aggregated" in md
