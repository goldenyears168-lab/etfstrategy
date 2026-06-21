"""copytrade_hypothesis · H1 假说验证。"""

from __future__ import annotations

import unittest

from research.backtest.copytrade_hypothesis import (
    filter_grouped_by_day_leg_count,
    leg_count_bucket,
)


class CopytradeHypothesisTests(unittest.TestCase):
    def test_leg_count_bucket(self) -> None:
        self.assertEqual(leg_count_bucket(1), "1")
        self.assertEqual(leg_count_bucket(3), "2-4")
        self.assertEqual(leg_count_bucket(7), "5-10")
        self.assertEqual(leg_count_bucket(12), "11+")

    def test_filter_grouped_by_day_leg_count(self) -> None:
        grouped = {
            "d1": [object(), object()],
            "d2": [object()] * 6,
            "d3": [object()] * 12,
        }
        out = filter_grouped_by_day_leg_count(
            grouped,
            lambda n: not (5 <= n <= 10),
        )
        self.assertIn("d1", out)
        self.assertNotIn("d2", out)
        self.assertIn("d3", out)

    def test_primary_alpha_improved_uses_total(self) -> None:
        from research.backtest.copytrade_backtest import primary_alpha_improved

        base = {"total_alpha_ntd": 100.0, "recycled_total_alpha_ntd": 50.0}
        better_total = {"total_alpha_ntd": 120.0, "recycled_total_alpha_ntd": 40.0}
        self.assertTrue(primary_alpha_improved(better_total, base))


if __name__ == "__main__":
    unittest.main()
