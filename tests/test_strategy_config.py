"""strategy_config · Strategy 層 registry 測試。"""

from __future__ import annotations

import unittest

from strategy_config import load_strategy_config, validate_strategies_alignment


class StrategyConfigTests(unittest.TestCase):
    def test_load_strategy_config(self) -> None:
        cfg = load_strategy_config()
        self.assertEqual(cfg.layer, "strategy")
        ids = cfg.strategy_ids()
        self.assertIn("00981a-l1h9", ids)
        self.assertIn("rrg-mono-hold7", ids)
        self.assertIn("vcp-pivot-gate", ids)
        self.assertEqual(len(ids), 5)

    def test_adopted_spec_fields(self) -> None:
        cfg = load_strategy_config()
        l1h9 = cfg.get("00981a-l1h9")
        assert l1h9 is not None
        self.assertEqual(l1h9.schedule, "manual")
        self.assertEqual(l1h9.n_slots, 9)
        self.assertEqual(l1h9.hold_days, 9)

    def test_backtest_blocks_in_strategy_yaml(self) -> None:
        cfg = load_strategy_config()
        for sid in ("00981a-l1h9", "rrg-mono-hold7"):
            spec = cfg.get(sid)
            assert spec is not None
            assert spec.backtest is not None
            self.assertTrue(spec.backtest.spec_type)
            self.assertGreater(len(spec.backtest.metrics), 0)
        l1 = cfg.get("00981a-l1h9")
        assert l1 and l1.backtest
        self.assertEqual(l1.backtest.params.get("hold_days"), 9)

    def test_strategies_alignment(self) -> None:
        missing = validate_strategies_alignment()
        self.assertEqual(missing, [])

    def test_enabled_alignment_with_registry(self) -> None:
        from strategy_registry import load_strategy_registry

        cfg = load_strategy_config()
        reg = load_strategy_registry()
        for sid in cfg.strategy_ids():
            adopted = cfg.get(sid)
            registry = reg.get(sid)
            assert adopted is not None and registry is not None, sid
            self.assertEqual(
                adopted.enabled,
                registry.enabled,
                f"{sid}: strategy.yaml vs strategies.yaml enabled mismatch",
            )

    def test_hub_strategies_shape(self) -> None:
        cfg = load_strategy_config()
        hub = cfg.hub_strategies()
        self.assertIn("00981a-l1h9", hub)
        self.assertEqual(hub["00981a-l1h9"]["title"], cfg.get("00981a-l1h9").title)


if __name__ == "__main__":
    unittest.main()
