"""unified comparison layer · config and metrics tests."""

from __future__ import annotations

import unittest

from backtest_standard_config import load_backtest_standard_config
from research.backtest.unified_comparison import _interval_metrics


class UnifiedComparisonTests(unittest.TestCase):
    def test_load_backtest_standard(self) -> None:
        cfg = load_backtest_standard_config()
        self.assertEqual(cfg.benchmark, "IX0001")
        self.assertEqual(cfg.comparison_notional_ntd, 100_000.0)
        self.assertIn("00981a-l1h9", cfg.track_adapters)
        self.assertIn("cmp_max_common", cfg.windows)
        hold7 = cfg.track_adapters["rrg-mono-hold7"]
        self.assertEqual(hold7.total_capital_ntd(cfg.comparison_notional_ntd), 100_000.0)

    def test_interval_metrics_excess(self) -> None:
        nav = [
            {"date": "2026-01-02", "equity_ntd": 100.0, "benchmark_equity_ntd": 100.0},
            {"date": "2026-01-03", "equity_ntd": 110.0, "benchmark_equity_ntd": 105.0},
        ]
        m = _interval_metrics(nav, total_capital=100.0, min_days_for_cagr=120)
        self.assertEqual(m["total_return_pct"], 10.0)
        self.assertAlmostEqual(m["benchmark_return_pct"], 5.0)
        self.assertAlmostEqual(m["interval_excess_pct"], 5.0)
        self.assertIsNone(m["cagr_pct"])


if __name__ == "__main__":
    unittest.main()
