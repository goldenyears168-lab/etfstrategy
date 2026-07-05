"""Tests for RRG lane OOS graduation (P4)."""

from __future__ import annotations

import pandas as pd

from research.backtest.rrg_lane_oos_graduation import (
    _g3_pass,
    _lane_ret_stats,
    _quality_pass,
)


def test_lane_ret_stats_empty():
    st = _lane_ret_stats(pd.DataFrame(), 1.0)
    assert st["n"] == 0


def test_g3_pass_mid_zones():
    rows = [
        {"breadth_zone": "neutral", "n": 20, "mean_3d": 1.5},
        {"breadth_zone": "strong", "n": 15, "mean_3d": 2.0},
        {"breadth_zone": "overbought", "n": 10, "mean_3d": -0.5},
    ]
    ok, fail = _g3_pass(rows, mid_zones=("neutral", "strong", "overbought"), fail_mean=0.0, min_n=8)
    assert ok is True
    assert fail == ["overbought"]


def test_quality_pass():
    th = {"win_3d_min": 0.5, "mean_3d_min": 2.0, "n_min": 10}
    assert _quality_pass({"n": 20, "win_3d": 0.6, "mean_3d": 3.0}, th)
    assert not _quality_pass({"n": 5, "win_3d": 0.6, "mean_3d": 3.0}, th)
