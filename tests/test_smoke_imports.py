"""Smoke imports — catch deleted-module regressions in CI."""

from __future__ import annotations

import unittest


class SmokeImports(unittest.TestCase):
    def test_daily_pipeline_modules(self) -> None:
        import etf_daily_report  # noqa: F401
        import regime_daily_brief  # noqa: F401
        import research_config  # noqa: F401
        import strategy_config  # noqa: F401

    def test_research_backtest_modules(self) -> None:
        from research.backtest import slot_backtest_summary  # noqa: F401

    def test_strategy_config_loads(self) -> None:
        from strategy_config import load_strategy_config

        cfg = load_strategy_config()
        self.assertEqual(cfg.layer, "strategy")
        self.assertIn("00981a-l1h9", cfg.strategy_ids())

    def test_research_config_loads(self) -> None:
        from research_config import load_research_config

        cfg = load_research_config()
        self.assertEqual(cfg.layer, "research")
        self.assertIn("copytrade-hypothesis-matrix", cfg.topic_ids())


if __name__ == "__main__":
    unittest.main()
