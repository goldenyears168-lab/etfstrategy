"""RRG WMA / quadrant classification."""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from rrg_rotation import (
    classify_quadrant,
    compute_rrg_panel,
    rs_ratio_momentum,
    wma,
)


class TestRrgRotation(unittest.TestCase):
    def test_wma_known_values(self) -> None:
        s = pd.Series([1.0, 2.0, 3.0, 4.0])
        out = wma(s, 3)
        self.assertTrue(math.isnan(out.iloc[0]))
        self.assertTrue(math.isnan(out.iloc[1]))
        # weights 1,2,3 on [1,2,3] => 14/6
        self.assertAlmostEqual(float(out.iloc[2]), 14.0 / 6.0, places=6)

    def test_classify_quadrant(self) -> None:
        self.assertEqual(classify_quadrant(101, 101), "leading")
        self.assertEqual(classify_quadrant(101, 99), "weakening")
        self.assertEqual(classify_quadrant(99, 99), "lagging")
        self.assertEqual(classify_quadrant(99, 101), "improving")
        self.assertIsNone(classify_quadrant(float("nan"), 100))

    def test_rs_ratio_momentum_baseline_near_100(self) -> None:
        idx = pd.date_range("2024-01-01", periods=80, freq="B")
        asset = pd.Series(np.linspace(100, 120, len(idx)), index=idx)
        bench = pd.Series(np.linspace(100, 110, len(idx)), index=idx)
        ratio, mom = rs_ratio_momentum(asset, bench, length=10)
        tail_r = float(ratio.dropna().iloc[-1])
        tail_m = float(mom.dropna().iloc[-1])
        self.assertGreater(tail_r, 100)
        self.assertTrue(np.isfinite(tail_m))

    def test_compute_rrg_panel_shape(self) -> None:
        idx = [f"2024-01-{d:02d}" for d in range(1, 41)]
        close = pd.DataFrame(
            {
                "2330": np.linspace(500, 600, 40),
                "2317": np.linspace(100, 90, 40),
            },
            index=idx,
        )
        bench = pd.Series(np.linspace(18000, 18500, 40), index=idx)
        ratio, mom, quad = compute_rrg_panel(close, bench, length=10)
        self.assertEqual(ratio.shape, close.shape)
        self.assertEqual(mom.shape, close.shape)
        self.assertEqual(quad.shape, close.shape)
        last_q = quad.iloc[-1]["2330"]
        self.assertIn(last_q, ("leading", "weakening", "lagging", "improving", None))


if __name__ == "__main__":
    unittest.main()
