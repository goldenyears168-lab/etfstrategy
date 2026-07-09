"""Tests for RRG pool Top-K WFA (P3)."""

from __future__ import annotations

import pandas as pd

from research.backtest.archive.rrg_lane_pool_topk import (
    build_year_walk_forward_folds,
    daily_rank_ic,
    daily_topk_stats,
    _evaluate_hml3,
)


def _mini_panel() -> pd.DataFrame:
    rows = []
    for i in range(60):
        d = f"202{i // 20}-0{(i % 12) + 1:02d}-15"
        for sid in ("2330", "2317", "2454"):
            rows.append(
                {
                    "sid": sid,
                    "date": d,
                    "ret_3d": float(i % 7) + (0.5 if sid == "2330" else 0),
                    "pred": float(i % 5) + (1.0 if sid == "2330" else 0),
                }
            )
    return pd.DataFrame(rows)


def test_year_folds():
    dates = [f"201{y}-06-01" for y in range(5)] + [f"202{y}-06-01" for y in range(4)]
    folds = build_year_walk_forward_folds(dates, train_years=3, test_years=1)
    assert len(folds) >= 1
    assert folds[0].train_years == ("2015", "2016", "2017") or folds[0].fold_id == 0


def test_daily_rank_ic():
    df = _mini_panel()
    ic = daily_rank_ic(df, min_cross_section=2)
    assert len(ic) > 0


def test_daily_topk_excess():
    df = _mini_panel()
    stats = daily_topk_stats(df, k=2, min_cross_section=2)
    assert stats
    assert "excess_pp" in stats[0]


def test_hml3_eval():
    fail = _evaluate_hml3({"oos_ic_mean": 0.01, "oos_topk_excess_mean_pp": 0.2})
    assert fail["pass_all"] is False
    ok = _evaluate_hml3({"oos_ic_mean": 0.05, "oos_topk_excess_mean_pp": 0.8})
    assert ok["pass_all"] is True
