"""Tests for C18acc avoid spread_mixed slot re-sim helpers."""

from research.backtest.archive.c18acc_avoid_mixed_slot_resim import (
    _slice_periods,
    _spread_at_first_poll,
    _window_stats,
)


def test_spread_at_first_poll_skips_before_no_trade():
    frames = [
        {"date": "2026-01-02", "minute": "09:00"},
        {"date": "2026-01-02", "minute": "09:30"},
    ]
    points = [None, {"spread_class": "mixed"}]
    assert _spread_at_first_poll(frames, points, trade_date="2026-01-02") == "mixed"


def test_spread_at_first_poll_aligned():
    frames = [{"date": "2026-01-02", "minute": "09:35"}]
    points = [{"spread_class": "aligned"}]
    assert _spread_at_first_poll(frames, points, trade_date="2026-01-02") == "aligned"


def test_slice_and_window_stats():
    periods = [
        {
            "entry_date": "2025-06-01",
            "return_pct": 10.0,
            "bench_return_pct": 1.0,
            "gross_win": True,
            "beat_bench": True,
        },
        {
            "entry_date": "2026-01-05",
            "return_pct": -2.0,
            "bench_return_pct": 0.5,
            "gross_win": False,
            "beat_bench": False,
        },
    ]
    oos = _slice_periods(periods, start="2026-01-01", end="2026-12-31")
    assert len(oos) == 1
    stats = _window_stats(oos)
    assert stats["n_periods"] == 1
    assert stats["mean_excess_pct"] == -2.5
