"""Tests for overnight_gap_seg_calibration."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.backtest.overnight_gap_seg_calibration import (
    _assign_quintile,
    _bucket_stats,
    _build_oos_split,
    _quintile_table,
    _run_hypothesis_tests,
)


def test_bucket_stats_basic():
    st = _bucket_stats([0.5, 1.0, -0.5, 2.0, -1.0])
    assert st is not None
    assert st.n == 5
    assert st.p_gap_up == 60.0
    assert st.mean_gap_pct == 0.4


def test_quintile_table_monotonic_spread():
    rng = pd.Series([1, 2, 3, 4, 5] * 40)
    gap = rng * 0.5 + pd.Series(range(200)) * 0.001
    df = pd.DataFrame({"seg_last_daily": rng, "overnight_gap_pct": gap})
    tbl = _quintile_table(df, "seg_last_daily")
    assert tbl["n"] == 200
    assert tbl["spread_top_minus_bottom_pp"] > 0
    assert tbl["mannwhitney_p"] < 0.05


def test_oos_split_structure():
    df = pd.DataFrame(
        {
            "trade_date": [f"2024-01-{d:02d}" for d in range(1, 21) for _ in range(5)],
            "seg_last_daily": np.random.randn(100),
            "seg_last_intraday_1300": np.random.randn(100),
            "overnight_gap_pct": np.random.randn(100) * 2,
        }
    )
    oos = _build_oos_split(df, 0.7)
    assert "train" in oos and "test" in oos
    assert oos["train"]["n"] > oos["test"]["n"]


def test_hypothesis_tests_keys():
    df = pd.DataFrame(
        {
            "seg_last_daily": list(range(100)),
            "seg_last_intraday_1300": list(range(100)),
            "overnight_gap_pct": [x * 0.01 for x in range(100)],
        }
    )
    ht = _run_hypothesis_tests(df)
    assert "H1_sd_quintile" in ht
    assert "significant_at_005" in ht
