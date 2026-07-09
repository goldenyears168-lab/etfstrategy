"""Tests for RRG × Regime four-axis Phase 0 annot helpers."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.archive.rrg_regime_four_axis_annot import (
    _bench_bucket,
    _outcome_compare,
    _stage2_bucket,
    _universe_rrg_stats_from_quad,
    annotate_lifecycle_event,
)


class TestRrgRegimeFourAxisAnnot(unittest.TestCase):
    def test_bench_bucket(self) -> None:
        self.assertEqual(_bench_bucket(2.5), "b2")
        self.assertEqual(_bench_bucket(1.2), "b1")
        self.assertEqual(_bench_bucket(0.0), "b0")
        self.assertEqual(_bench_bucket(-0.5), "neg")

    def test_stage2_bucket(self) -> None:
        self.assertEqual(_stage2_bucket(65.0), "high")
        self.assertEqual(_stage2_bucket(50.0), "mid")
        self.assertEqual(_stage2_bucket(30.0), "low")

    def test_outcome_compare(self) -> None:
        events = [
            {"flag": True, "nxt_ret": 1.0},
            {"flag": True, "nxt_ret": 0.5},
            {"flag": False, "nxt_ret": -0.5},
            {"flag": False, "nxt_ret": -1.0},
        ]
        row = _outcome_compare(events, flag_field="flag")
        self.assertEqual(row["flagged_n"], 2)
        self.assertEqual(row["unflagged_n"], 2)
        self.assertEqual(row["flagged_mean"], 0.75)
        self.assertEqual(row["unflagged_mean"], -0.75)
        self.assertEqual(row["delta_pp"], 1.5)

    def test_universe_rrg_stats_from_quad(self) -> None:
        quad = pd.DataFrame(
            {
                "A": ["improving", "leading"],
                "B": ["improving", "improving"],
                "C": ["lagging", "weakening"],
            },
            index=["2024-01-02", "2024-01-03"],
        )
        stats = _universe_rrg_stats_from_quad(quad)
        self.assertEqual(stats["2024-01-02"]["dominant_quadrant"], "improving")
        self.assertAlmostEqual(stats["2024-01-02"]["rotation_health_pct"], 66.7, places=1)

    def test_annotate_lifecycle_event(self) -> None:
        row = {
            "date": "2024-06-03",
            "sid": "2330",
            "end_q": "improving",
            "prev_q": "lagging",
            "sig_ret": 0.5,
            "nxt_ret": 1.2,
            "bench_today_ret": -0.3,
            "bench_nxt_ret": 0.8,
            "s0_fresh_flip": True,
            "s1_early": True,
            "s2_setup_core": False,
            "s3_burst": False,
            "improve_streak": 1,
            "rv": 99.1,
        }
        out = annotate_lifecycle_event(
            row,
            prior_date="2024-05-31",
            breadth_zone_200="neutral",
            trend_posture="broadening",
            weinstein_stage=2,
            stage2_pass_pct=55.0,
            universe_rrg={"dominant_quadrant": "improving", "rotation_health_pct": 62.0, "improving_pct": 35.0},
        )
        self.assertEqual(out["bench_bucket"], "neg")
        self.assertEqual(out["breadth_zone_200"], "neutral")
        self.assertEqual(out["excess_nxt"], 0.4)
        self.assertTrue(out["s0_fresh_flip"])
        self.assertFalse(out["bench_b0"])


if __name__ == "__main__":
    unittest.main()
