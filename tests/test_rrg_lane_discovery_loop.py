"""Tests for rrg_lane_discovery_loop · mask + loss gate (no DB)."""

from __future__ import annotations

import pandas as pd

from research.backtest.rrg_lane_discovery_loop import (
    TOPIC_ID,
    _expand_seed_grid,
    build_miss_list,
    evaluate_seed,
    mask_from_seed,
    merge_miss_frames,
)
from research_config import load_research_config


def test_topic_registered():
    cfg = load_research_config()
    topic = cfg.get(TOPIC_ID)
    assert topic is not None
    assert topic.sweep is not None
    assert "H1c" in topic.sweep.get("seeds", {})
    assert "H3" in topic.sweep.get("seeds", {})


def test_expand_seed_grid_nonempty():
    cfg = load_research_config()
    topic = cfg.get(TOPIC_ID)
    grid = _expand_seed_grid(topic.sweep or {})
    assert len(grid) >= 10
    assert any(c.get("seed_id") == "H1c" for c in grid)
    assert any(c.get("seed_id") == "H3" for c in grid)


def test_mask_from_seed_h1c():
    panel = pd.DataFrame(
        {
            "sig_ret": [-3.0, -1.5, 0.5],
            "excess": [-1.0, -0.5, 0.0],
            "end_q": ["lagging", "lagging", "improving"],
            "prev_end_q": ["lagging", "lagging", "lagging"],
            "bench_today_ret": [2.0, 1.0, 2.0],
            "rv": [97.0, 98.5, 97.0],
            "improve_streak": [0, 0, 5],
            "session_gap_days": [1, 1, 1],
        }
    )
    m = mask_from_seed(
        panel,
        {
            "seed_id": "H1c",
            "bench_min": 1.5,
            "sig_lo": -5,
            "sig_hi": -1,
            "rv_lo": 96,
            "rv_hi": 99,
            "require_elag_plag": True,
            "require_excess_band": True,
            "require_gap1": True,
        },
    )
    assert m.tolist() == [True, False, False]


def test_evaluate_seed_loss_gate():
    panel = pd.DataFrame(
        {
            "date": ["2020-01-01"] * 4,
            "ret_3d": [5.0, 2.0, -6.0, 1.0],
            "min_ret_3d": [3.0, 1.0, -7.0, -9.0],
            "sig_ret": [-2.0] * 4,
            "excess": [-1.0] * 4,
            "end_q": ["lagging"] * 4,
            "prev_end_q": ["lagging"] * 4,
            "bench_today_ret": [2.5] * 4,
            "rv": [97.0] * 4,
            "improve_streak": [0] * 4,
            "session_gap_days": [1] * 4,
        }
    )
    r = evaluate_seed(
        panel,
        {"seed_id": "H1c", "bench_min": 2.0, "require_elag_plag": True, "require_gap1": True},
        gates={"mean_3d_min": 2.0, "win_3d_min": 0.5, "loss5_rate_max": 0.25, "n_min": 2},
    )
    assert r["n"] == 4
    assert r["loss5_rate"] == 0.25
    assert r["loss8_path_rate"] == 0.25


def test_build_miss_list_uses_panel_integer_index():
    """Regression: bool(NaN) must not treat failed loc as lane hit."""
    panel = pd.DataFrame(
        {
            "sid": ["2330", "2330"],
            "date": ["2020-06-01", "2020-06-02"],
            "sig_ret": [-1.0, 3.0],
            "excess": [-0.5, 1.0],
            "end_q": ["improving", "improving"],
            "prev_end_q": ["improving", "improving"],
            "bench_today_ret": [2.5, 2.5],
            "rv": [99.0, 99.0],
            "improve_streak": [8, 8],
            "session_gap_days": [1, 1],
            "disp_contract": [False, False],
            "sid_hist_ok": [True, True],
            "ret_3d": [10.0, 2.0],
        }
    )
    lane_defs = [
        {
            "id": "R99",
            "rule_atoms": ["q45"],
        }
    ]
    cases = pd.DataFrame(
        [
            {
                "sid": "2330",
                "signal_date": "2020-06-01",
                "ret_3d": 10.0,
                "in_pre_release_pool": True,
            }
        ]
    )
    miss = build_miss_list(cases, lane_defs, panel, min_ret_3d=5.0)
    assert len(miss) == 1
    assert miss.iloc[0]["sid"] == "2330"
    assert miss.iloc[0]["matched_lanes"] == []


def test_merge_miss_frames_dedupes():
    a = pd.DataFrame([{"sid": "1", "signal_date": "2020-01-01", "ret_3d": 6.0}])
    b = pd.DataFrame([{"sid": "1", "signal_date": "2020-01-01", "ret_3d": 8.0}])
    m = merge_miss_frames(a, b)
    assert len(m) == 1
    assert float(m.iloc[0]["ret_3d"]) == 8.0
