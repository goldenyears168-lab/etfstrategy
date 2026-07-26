"""Tests for C18acc vs ABC v3+f1 comparison bundle."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

from research.backtest.archive.c18acc_abc_v3_f1_comparison import (
    _event_stats_from_pct_values,
    enrich_event_level,
    render_c18acc_abc_v3_f1_comparison_md,
    select_executed_slot_periods,
)


def test_select_executed_slot_periods_respects_slot_limit():
    conn = MagicMock(spec=sqlite3.Connection)
    close = MagicMock()
    close.columns = ["2330"]
    close.index = ["2025-01-02", "2025-01-03", "2025-01-06"]
    close.at = MagicMock(return_value=100.0)

    periods = [
        {"stock_id": "2330", "entry_date": "2025-01-02", "exit_date": "2025-01-03", "entry_px": 100.0},
        {"stock_id": "2454", "entry_date": "2025-01-02", "exit_date": "2025-01-06", "entry_px": 200.0},
        {"stock_id": "2317", "entry_date": "2025-01-03", "exit_date": "2025-01-06", "entry_px": 150.0},
    ]
    with patch("research.backtest.archive.c18acc_abc_v3_f1_comparison._resolve_entry_px", return_value=100.0):
        executed = select_executed_slot_periods(
            conn,
            close,
            ["2025-01-02", "2025-01-03", "2025-01-06"],
            periods,
            total_capital=30_000.0,
            n_slots=1,
            cost_model=None,
        )
    assert len(executed) == 2


def test_event_stats_from_pct_values():
    stats = _event_stats_from_pct_values([2.0, -1.0, 1.0], hold_days=3)
    assert stats["n"] == 3
    assert stats["mean_ret_net_pct"] == 0.667
    assert stats["win_rate_pct"] == 66.67
    assert stats["mean_daily_ret_net_pct"] == 0.2222


def test_enrich_event_level_from_ledger():
    payload = {
        "abc_f1_trade_ledger": [{"net_pct": 2.85}, {"net_pct": -1.0}],
        "c18acc_trade_ledger": [{"return_pct": 5.0}],
    }
    enriched = enrich_event_level(payload)
    abc_exec = enriched["event_level"]["abc_f1"]["executed_topk"]
    assert abc_exec["mean_ret_net_pct"] == 0.925
    assert enriched["event_level"]["champion_pipeline"]["mean_ret_3d_net_pct"] == 2.845


def test_render_md_includes_event_level_section():
    payload = {
        "generated_at": "2026-07-08",
        "total_runtime_s": 1.0,
        "date_start": "2024-01-01",
        "date_end": "2024-01-31",
        "n_trade_dates": 20,
        "comparison_contract": {"notional_ntd": 100_000, "cost_model": {"enabled": True}},
        "comparison": {
            "winner_daily": "rrg-mono-swap-accel",
            "winner_ann_mean": "abc-v3-f1-pullback",
            "abc_f1_signal_capture_pct": 59.6,
        },
        "strategies": {
            "rrg-mono-swap-accel": {"n_slots": 3, "portfolio": {}},
            "abc-v3-f1-pullback": {"n_slots": 5, "hold_days": 3, "n_candidates": 688, "portfolio": {}},
        },
        "event_level": {
            "champion_pipeline": {"label": "90d OOS pipeline", "n": 170, "mean_ret_3d_net_pct": 2.845,
                                   "win_rate_pct": 54.71, "median_ret_3d_net_pct": 0.561,
                                   "val_mean_ret_3d_net_pct": 1.826,
                                   "source": "reports/research/rrg/20260708_abc_v3_f1_pipeline_90d.json"},
            "abc_f1": {
                "all_candidates": {"n": 688, "mean_ret_net_pct": 2.1, "mean_daily_ret_net_pct": 0.7,
                                   "win_rate_pct": 53.0, "median_ret_net_pct": 0.4},
                "executed_topk": {"n": 410, "mean_ret_net_pct": 1.236, "win_rate_pct": 50.0},
                "hold_days": 3,
            },
            "c18acc": {"n": 187, "mean_ret_net_pct": 6.661, "win_rate_pct": 66.3,
                       "hold_note": "5–10d + rotation/S2"},
        },
        "process_log": [{"ts": "t", "elapsed_s": 0, "phase": "init", "detail": "ok"}],
        "c18acc_trade_ledger": [{"idx": 1, "stock_id": "2330", "stock_name": "台積電"}],
        "abc_f1_trade_ledger": [{"idx": 1, "stock_id": "2454", "stock_name": "聯發科"}],
    }
    md = render_c18acc_abc_v3_f1_comparison_md(payload)
    assert "事件層績效" in md
    assert "2.845" in md
    assert "組合績效（100k NAV" in md
    assert "C18acc 交易紀錄" in md
    assert "ABC v3+f1 交易紀錄" in md
    assert "執行過程紀錄" in md


def test_render_md_includes_trade_ledger_headers_legacy():
    payload = {
        "generated_at": "2026-07-08",
        "total_runtime_s": 1.0,
        "date_start": "2024-01-01",
        "date_end": "2024-01-31",
        "n_trade_dates": 20,
        "comparison_contract": {"notional_ntd": 100_000, "cost_model": {"enabled": True}},
        "comparison": {"winner_daily": "rrg-mono-swap-accel", "winner_ann_mean": "abc-v3-f1-pullback"},
        "strategies": {
            "rrg-mono-swap-accel": {"n_slots": 3, "portfolio": {}},
            "abc-v3-f1-pullback": {"n_slots": 5, "hold_days": 3, "portfolio": {}},
        },
        "process_log": [{"ts": "t", "elapsed_s": 0, "phase": "init", "detail": "ok"}],
        "c18acc_trade_ledger": [{"idx": 1, "stock_id": "2330", "stock_name": "台積電"}],
        "abc_f1_trade_ledger": [{"idx": 1, "stock_id": "2454", "stock_name": "聯發科"}],
    }
    md = render_c18acc_abc_v3_f1_comparison_md(payload)
    assert "C18acc 交易紀錄" in md
    assert "ABC v3+f1 交易紀錄" in md
    assert "執行過程紀錄" in md
