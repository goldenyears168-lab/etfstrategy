"""Tests for overnight_rrg_signal_test."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.backtest.overnight_rrg_signal_test import (
    _compound_pct,
    _event_flags,
    compare_signal_pair,
    simulate_overnight_ew_compound,
    summarize_excess_series,
)


def test_compound_pct():
    assert _compound_pct([1.0, 1.0]) == 2.01


def test_overnight_ew_compound():
    df = pd.DataFrame(
        {
            "trade_date": ["2024-01-02", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
            "ev_leading": [True, True, False, True, False],
            "ret_overnight": [2.0, 0.0, 1.0, -1.0, 0.5],
            "bench_ret_overnight": [1.0, 1.0, 0.5, 0.5, 0.5],
        }
    )
    r = simulate_overnight_ew_compound(df, "leading")
    assert r["total_return_pct"] == -0.01
    assert r["n_invested_days"] == 2


def test_event_flags_lagging_up_left_and_enter():
    feat = {
        "end_q": "lagging",
        "trend": "up_left",
        "seg_last": 0.5,
        "quadrants": ["weakening", "lagging"],
    }
    flags = _event_flags(feat, seg_q=4)
    assert flags["lagging_up_left"] is True
    assert flags["enter_lagging"] is True
    assert flags["seg_last_q4"] is True


def test_compare_signal_pair_a_beats_b():
    df = pd.DataFrame(
        {
            "ev_a": [True] * 30 + [False] * 30,
            "ev_b": [False] * 30 + [True] * 30,
            "ex_overnight": [0.5] * 30 + [-0.2] * 30,
        }
    )
    c = compare_signal_pair(df, "a", "b", horizon="overnight")
    assert c["a_beats_b"] is True
    assert c["significant_005"] is True


def test_summarize_positive_excess_significant():
    excess = [0.5] * 50 + [-0.1] * 10
    s = summarize_excess_series(excess)
    assert s["n"] == 60
    assert s["mean_excess_pct"] > 0
    assert s["significant_005"] is True


def test_summarize_zero_mean_not_significant():
    rng = np.random.default_rng(0)
    excess = rng.normal(0, 1, 80).tolist()
    s = summarize_excess_series(excess)
    assert s["n"] == 80
    assert s["significant_005"] is False
