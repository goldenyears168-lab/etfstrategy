"""Tests for ABC f1 + C18 S2 exit overlay."""

from __future__ import annotations

from research.backtest.archive.abc_f1_c18_s2_exit_overlay import (
    _abc_hold3d_rows,
    _leg_stats_from_rows,
    render_abc_f1_c18_s2_exit_overlay_md,
)


def test_abc_hold3d_rows_from_candidates():
    rows = _abc_hold3d_rows(
        [
            {
                "stock_id": "2330",
                "entry_date": "2025-01-02",
                "net_pct": 2.5,
                "source_strategy": "abc_v3_f1_skip09",
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0]["exit_mode"] == "hold_3d"
    assert rows[0]["hold_days"] == 3


def test_leg_stats_daily_from_hold():
    stats = _leg_stats_from_rows(
        [
            {"net_pct": 9.0, "hold_days": 9},
            {"net_pct": 3.0, "hold_days": 3},
        ]
    )
    assert stats["n"] == 2
    assert stats["mean_ret_net_pct"] == 6.0
    assert stats["mean_hold_days"] == 6.0
    assert stats["mean_daily_ret_net_pct"] == 1.0


def test_render_overlay_md():
    payload = {
        "window": {"date_start": "2026-01-01", "date_end": "2026-04-01", "n_trading_days": 60},
        "contract": {"abc_entry": "f1", "s2_exit": "s2", "c18_reference": "c18"},
        "champion_reference": {"n": 170, "mean_ret_3d_net_pct": 2.845},
        "variants": {
            "abc_f1_hold3d": {
                "label": "hold3d",
                "full": {"n": 10, "mean_ret_net_pct": 2.5, "mean_hold_days": 3, "win_rate_pct": 50},
                "exit_reasons": {"hold_3d": 10},
            },
            "abc_f1_s2_single": {
                "label": "s2",
                "full": {"n": 10, "mean_ret_net_pct": 5.0, "mean_hold_days": 8, "win_rate_pct": 60},
                "exit_reasons": {"quad_force_exit": 6, "max_hold": 4},
            },
            "c18_s2_actual": {
                "label": "c18",
                "full": {"n": 5, "mean_ret_net_pct": 8.0, "mean_hold_days": 9, "win_rate_pct": 80},
                "exit_reasons": {"score_swap": 2},
            },
        },
        "verdict": {
            "summary": "test",
            "delta_s2_vs_hold3d_pp": 2.5,
            "delta_s2_vs_c18_actual_pp": -3.0,
            "s2_beats_hold3d_mean": True,
            "reasons": [],
        },
    }
    md = render_abc_f1_c18_s2_exit_overlay_md(payload)
    assert "ABC f1 + S2 single" in md
    assert "quad_force_exit" in md
