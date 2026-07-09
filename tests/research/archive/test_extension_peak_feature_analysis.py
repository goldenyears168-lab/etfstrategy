"""Tests for extension peak feature analysis · stats helpers."""

from __future__ import annotations

import unittest

from research.backtest.archive.extension_peak_feature_analysis import (
    benjamini_hochberg,
    fisher_exact_p,
    wilson_ci,
)


class TestExtensionPeakStats(unittest.TestCase):
    def test_wilson_ci_contains_sample_p(self) -> None:
        lo, hi = wilson_ci(920, 10000)
        self.assertLess(lo, 0.092)
        self.assertGreater(hi, 0.092)

    def test_fisher_exact_significant_lift(self) -> None:
        p = fisher_exact_p(500, 500, 50, 9500)
        self.assertLess(p, 0.001)

    def test_benjamini_hochberg_monotone(self) -> None:
        q = benjamini_hochberg([0.001, 0.04, 0.5])
        self.assertLessEqual(q[0], q[1])
        self.assertLessEqual(q[1], q[2])


if __name__ == "__main__":
    unittest.main()
