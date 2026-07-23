"""Tests for C18acc hold 3d event study."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

from research.backtest.archive.c18acc_hold3d_event_study import (
    _event_stats_from_nets,
    c18acc_live_s2_config,
    compute_hold_nd_event_leg,
    render_c18acc_hold3d_event_study_md,
)


def test_c18acc_live_s2_config_uses_poll_5m_champion():
    cfg = c18acc_live_s2_config(n_slots=3)
    assert cfg.timing_mode == "poll_5m"
    assert cfg.candidate_pool == "fresh_union_accel"
    assert cfg.quad_force_exit_mode == "weakening"
    assert cfg.n_slots == 3


def test_event_stats_from_nets():
    stats = _event_stats_from_nets([3.0, -1.0, 2.0], hold_days=3)
    assert stats["n"] == 3
    assert stats["mean_ret_net_pct"] == 1.333
    assert stats["win_rate_pct"] == 66.67
    assert stats["mean_daily_ret_net_pct"] == 0.4444


@patch("research.backtest.archive.c18acc_hold3d_event_study.kbar_close_at_minute")
@patch("research.backtest.archive.c18acc_hold3d_event_study.exit_close_date_from_entry")
def test_compute_hold_nd_event_leg(mock_exit_date, mock_kbar):
    import pandas as pd

    conn = MagicMock(spec=sqlite3.Connection)
    mock_exit_date.return_value = "2025-01-08"
    mock_kbar.side_effect = lambda _c, sid, d, _m: {
        ("2330", "2025-01-02"): 100.0,
        ("2330", "2025-01-08"): 110.0,
        ("IX0001", "2025-01-02"): 1000.0,
        ("IX0001", "2025-01-08"): 1010.0,
    }.get((sid, d), None)
    bench = pd.Series({"2025-01-02": 1000.0, "2025-01-08": 1010.0})

    leg = compute_hold_nd_event_leg(
        conn,
        stock_id="2330",
        entry_date="2025-01-02",
        entry_minute="10:00",
        entry_px=100.0,
        hold_days=3,
        bench=bench,
    )
    assert leg is not None
    assert leg["entry_date"] == "2025-01-02"
    assert leg["exit_date"] == "2025-01-08"
    assert leg["net_pct"] == 9.415  # 10% gross - 0.585 cost


def test_render_md_contains_champion():
    payload = {
        "window": {"date_start": "2025-01-01", "date_end": "2025-04-01", "n_trading_days": 60},
        "champion_reference": {"n": 170, "mean_ret_3d_net_pct": 2.845, "win_rate_pct": 54.71},
        "c18acc": {
            "unconstrained_slots": 99,
            "hold3d": {
                "unconstrained": {"full": {"n": 50, "mean_ret_net_pct": 1.5, "win_rate_pct": 52.0}},
                "slot_3": {"full": {"n": 40, "mean_ret_net_pct": 1.8, "win_rate_pct": 55.0}},
                "pool_daily": {"full": {"n": 200, "mean_ret_net_pct": 0.8, "win_rate_pct": 48.0}},
            },
            "actual_exit_3slot": {"n": 40, "mean_ret_net_pct": 6.0, "hold_note": "not 3d"},
            "slot_capture_pct": 80.0,
        },
        "abc_f1": {"hold3d": {"full": {"n": 100, "mean_ret_net_pct": 2.2}, "val": {"n": 30}}},
        "verdict": {
            "summary": "test",
            "beats_champion_event_mean": False,
            "best_c18_hold3d_mean_pct": 1.8,
            "delta_best_c18_vs_champion_pp": -1.045,
            "reasons": ["line1"],
        },
        "contract": {"hold_days": 3, "exit_minute": "13:30", "cost_round_trip_pct": 0.585},
    }
    md = render_c18acc_hold3d_event_study_md(payload)
    assert "2.845" in md
    assert "hold 3d" in md.lower()
    assert "Unconstrained" in md
