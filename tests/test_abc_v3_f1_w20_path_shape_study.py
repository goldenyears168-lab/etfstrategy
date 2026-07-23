"""Tests for abc_v3_f1_w20_path_shape_study shape classification."""

from __future__ import annotations

import unittest

from research.backtest.abc_v3_f1_w20_path_shape_study import (
    classify_w20_path_shape,
    run_spread_dist_depth_closeout,
)


class TestClassifyW20PathShape(unittest.TestCase):
    def test_pullback_then_recover(self) -> None:
        self.assertEqual(classify_w20_path_shape(-0.2, 0.1), "pullback_then_recover")
        self.assertEqual(classify_w20_path_shape(-0.1, 0.0), "pullback_then_recover")

    def test_continued_pullback(self) -> None:
        self.assertEqual(classify_w20_path_shape(-0.2, -0.1), "continued_pullback")

    def test_no_pullback(self) -> None:
        self.assertEqual(classify_w20_path_shape(0.0, -0.5), "no_pullback")
        self.assertEqual(classify_w20_path_shape(0.2, 0.1), "no_pullback")

    def test_sharp_bounce_takes_priority(self) -> None:
        self.assertEqual(classify_w20_path_shape(-0.4, 0.5), "sharp_bounce")


class TestSpreadDistCloseout(unittest.TestCase):
    def test_closeout_always_rejects_hypothesis(self) -> None:
        legs = []
        for i, dist in enumerate([0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5] * 4):
            legs.append(
                {
                    "stock_id": f"{1000 + i}",
                    "entry_date": "2026-01-02",
                    "return_pct": float(i % 5) - 1.0,
                    "entry_t0": {"spread_dist": dist, "spread_class": "pullback"},
                    "timeline": [
                        {"ret_from_entry_pct": 0.0},
                        {"ret_from_entry_pct": float(i % 5) - 1.0},
                    ],
                    "peak_frame_idx": 1,
                    "peak_ret_pct": float(i % 5) - 1.0,
                }
            )
        payload = run_spread_dist_depth_closeout(legs, legs_source="unit")
        self.assertEqual(payload["closeout"]["verdict"], "rejected")
        self.assertEqual(payload["n_annotated"], 36)
        self.assertEqual(len(payload["terciles"]), 3)


if __name__ == "__main__":
    unittest.main()
