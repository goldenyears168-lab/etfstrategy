"""Tests for rrg_lane_signal_cooccurrence (no DB)."""

from __future__ import annotations

import pandas as pd

from research.backtest.rrg_lane_signal_cooccurrence import (
    analyze_tag_cooccurrence,
    apply_exclusion_mask,
    build_tagged_cohort,
    encode_signal_tags,
    label_outcome_row,
    pattern_to_seed,
    run_cooccurrence_analysis,
    tag_mask,
)
from research.backtest.rrg_lane_discovery_loop import evaluate_seed, mask_from_seed
from research_config import load_research_config


def _sample_panel() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "sid": ["A", "B", "C", "D", "E", "F"],
            "date": [
                "2020-01-10",
                "2020-02-10",
                "2020-03-10",
                "2020-04-10",
                "2020-05-10",
                "2020-06-10",
            ],
            "sig_ret": [-2.0, -2.5, -1.5, 4.0, -2.0, 0.5],
            "excess": [-1.0, -0.8, -0.5, 1.0, -1.0, 0.0],
            "end_q": ["lagging"] * 4 + ["improving", "improving"],
            "prev_end_q": ["lagging"] * 4 + ["improving", "improving"],
            "bench_today_ret": [2.2, 2.3, 1.5, -0.5, 2.1, 0.5],
            "rv": [98.5, 98.2, 97.0, 96.0, 98.0, 97.5],
            "improve_streak": [0, 0, 2, 1, 8, 8],
            "session_gap_days": [1, 1, 1, 1, 1, 2],
            "disp_contract": [True, False, False, False, False, False],
            "sid_hist_ok": [True, True, False, True, True, True],
            "ret_3d": [4.0, 3.5, 1.0, -6.0, 5.0, 2.0],
            "min_ret_3d": [2.0, 2.5, 0.5, -7.0, 4.0, 1.0],
        }
    )


def test_encode_signal_tags_lagv_dip():
    row = _sample_panel().iloc[0]
    tags = encode_signal_tags(row)
    assert "lagv" in tags
    assert "dip_deep" in tags
    assert "b2" in tags
    assert "rv98_99" in tags
    assert "gap1" in tags
    assert "ex" in tags


def test_label_outcome_success_loss_extreme():
    panel = _sample_panel()
    assert label_outcome_row(panel.iloc[0], success_min=3.0, exclude_extreme_pct=25.0) == "success"
    assert label_outcome_row(panel.iloc[3], success_min=3.0, exclude_extreme_pct=25.0) == "loss"
    assert label_outcome_row(panel.iloc[2], success_min=3.0, exclude_extreme_pct=25.0) == "neutral"


def test_apply_exclusion_removes_chase_and_bench_neg():
    panel = _sample_panel()
    base = pd.Series(True, index=panel.index)
    m = apply_exclusion_mask(base, panel, ["bench_neg", "chase"])
    assert not m.iloc[3]  # bench_neg
    assert m.iloc[0]  # not excluded


def test_cooccurrence_finds_recurring_pair():
    cohort = build_tagged_cohort(_sample_panel(), success_min=3.0)
    pairs = analyze_tag_cooccurrence(cohort, min_recurrence=2, min_lift=1.0, min_months=1)
    patterns = {p["pattern"] for p in pairs}
    assert any("lagv" in p for p in patterns)


def test_pattern_to_seed_evaluates_with_exclusion():
    panel = _sample_panel()
    seed = pattern_to_seed("lagv+dip_deep+b2", "CO99")
    m = mask_from_seed(panel, seed)
    assert int(m.sum()) >= 2
    r = evaluate_seed(
        panel,
        seed,
        gates={"mean_3d_min": 2.0, "n_min": 2, "loss5_rate_max": 0.5},
        exclusion_tags=["bench_neg"],
    )
    assert r["n"] >= 2
    assert r["seed_id"] == "CO99"


def test_run_cooccurrence_analysis_smoke():
    cfg = load_research_config().get("rrg-lane-discovery-loop").sweep.get("cooccurrence") or {}
    out = run_cooccurrence_analysis(_sample_panel(), {**cfg, "min_tag_n_exclusion": 1})
    assert out["cohort_summary"]["n_total"] == 6
    assert "active_exclusion_tags" in out
    assert isinstance(out["pattern_seeds"], list)


def test_topic_has_cooccurrence_config():
    topic = load_research_config().get("rrg-lane-discovery-loop")
    cooc = (topic.sweep or {}).get("cooccurrence") or {}
    assert cooc.get("enabled") is True
    assert cooc.get("success_min_ret_3d") == 3.0
