"""rank_stats：ICIR、Max Drawdown。"""

from __future__ import annotations

import unittest

from rank_stats import icir, max_drawdown_pct, spearman_correlation


class TestRankStats(unittest.TestCase):
    def test_icir_known(self) -> None:
        # mean=0.1, pstdev=0.1 → ICIR=1.0
        self.assertAlmostEqual(icir([0.0, 0.2]) or 0.0, 1.0, places=5)

    def test_icir_insufficient(self) -> None:
        self.assertIsNone(icir([]))
        self.assertIsNone(icir([0.5]))

    def test_icir_zero_stdev(self) -> None:
        self.assertIsNone(icir([0.3, 0.3, 0.3]))

    def test_max_drawdown_all_positive(self) -> None:
        self.assertEqual(max_drawdown_pct([1.0, 2.0, 0.5]), 0.0)

    def test_max_drawdown_decline(self) -> None:
        # 100 → 90 (-10%) → 81 (-10% from 90, -19% from peak)
        dd = max_drawdown_pct([-10.0, -10.0])
        self.assertIsNotNone(dd)
        assert dd is not None
        self.assertAlmostEqual(dd, 19.0, places=1)

    def test_max_drawdown_empty(self) -> None:
        self.assertIsNone(max_drawdown_pct([]))

    def test_spearman_monotone(self) -> None:
        rho = spearman_correlation([1, 2, 3], [10, 20, 30])
        self.assertAlmostEqual(rho or 0.0, 1.0, places=5)


if __name__ == "__main__":
    unittest.main()
