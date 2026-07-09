"""Tests for rrg_streak_cohort_panel · streak detection + label (no DB)."""

from __future__ import annotations

import pandas as pd

from research.backtest.archive.rrg_streak_cohort_panel import (
    STRONG_STREAK_PCT,
    compute_hit_strong_streak,
    detect_three_day_up_streaks,
)


def _synthetic_close() -> tuple[pd.DataFrame, list[str]]:
    dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
    close = pd.DataFrame(
        {
            "AAA": [100.0, 105.0, 110.0, 120.0, 118.0],
            "BBB": [50.0, 51.0, 52.0, 53.0, 54.0],
        },
        index=dates,
    )
    return close, dates


def test_detect_three_day_up_streaks_finds_strong_window():
    close, dates = _synthetic_close()
    streaks = detect_three_day_up_streaks(close, dates, year_start=2024, year_end=2024)
    assert len(streaks) >= 1
    aaa = streaks[streaks["sid"] == "AAA"].iloc[0]
    assert aaa["streak_start"] == "2024-01-03"
    assert aaa["streak_end"] == "2024-01-05"
    assert float(aaa["streak_total_pct"]) == 20.0  # 120/100 - 1


def test_detect_three_day_up_streaks_includes_weak_up_streaks():
    close, dates = _synthetic_close()
    streaks = detect_three_day_up_streaks(close, dates, year_start=2024, year_end=2024)
    bbb = streaks[streaks["sid"] == "BBB"]
    assert len(bbb) >= 1
    assert all(float(x) < STRONG_STREAK_PCT for x in bbb["streak_total_pct"])


def test_compute_hit_strong_streak_pool_and_non_pool():
    assert compute_hit_strong_streak(
        20.0, in_pre_release_pool=False, ret_3d=None, max_ret_3d=None
    )
    assert not compute_hit_strong_streak(
        10.0, in_pre_release_pool=False, ret_3d=None, max_ret_3d=None
    )
    assert compute_hit_strong_streak(
        20.0,
        in_pre_release_pool=True,
        ret_3d=6.0,
        max_ret_3d=6.0,
        min_ret_3d_hit=5.0,
    )
    assert not compute_hit_strong_streak(
        20.0,
        in_pre_release_pool=True,
        ret_3d=2.0,
        max_ret_3d=3.0,
        min_ret_3d_hit=5.0,
    )
    assert compute_hit_strong_streak(
        20.0,
        in_pre_release_pool=True,
        ret_3d=2.0,
        max_ret_3d=6.0,
        min_ret_3d_hit=5.0,
    )
