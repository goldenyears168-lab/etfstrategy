"""strategy_registry 單元測試。"""

from __future__ import annotations

import unittest
from pathlib import Path

from strategy_registry import (
    load_strategy_registry,
    resolve_source_name,
    resolve_strategy_sources,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CFG = ROOT / "config" / "strategies.yaml"


class StrategyRegistryTests(unittest.TestCase):
    def test_load_default_registry(self) -> None:
        reg = load_strategy_registry()
        self.assertEqual(reg.primary_strategy, "etf-daily")
        ids = {s.strategy_id for s in reg.strategies}
        self.assertIn("etf-daily", ids)
        self.assertIn("regime-daily", ids)
        spec = reg.get("etf-daily")
        assert spec is not None
        self.assertTrue(spec.enabled)
        self.assertEqual(spec.layer, "facts")
        self.assertNotIn("research-os", ids)
        self.assertNotIn("p6-tier-flow", ids)
        self.assertNotIn("shared-analytics", ids)

    def test_adopted_strategies_in_registry(self) -> None:
        reg = load_strategy_registry()
        for sid in (
            "00981a-l1h9",
            "rrg-mono-hold7",
            "rrg-mono-swap-accel",
            "vcp-pivot-gate",
            "minervini-sepa-basket",
        ):
            spec = reg.get(sid)
            assert spec is not None, sid
            self.assertEqual(spec.layer, "strategy")

    def test_adopted_strategies_not_trading(self) -> None:
        reg = load_strategy_registry()
        for sid in ("rrg-mono-hold7", "vcp-pivot-gate"):
            spec = reg.get(sid)
            assert spec is not None
            self.assertEqual(spec.layer, "strategy")
            self.assertFalse(spec.e0)

    def test_resolve_source_name(self) -> None:
        self.assertEqual(
            resolve_source_name("{date}_etf_daily.md", ref_date="2026-06-15"),
            "20260615_etf_daily.md",
        )

    def test_resolve_strategy_sources_aliases(self) -> None:
        reg = load_strategy_registry()
        spec = reg.get("etf-daily")
        assert spec is not None
        pairs = resolve_strategy_sources(spec, ref_date="2026-06-15")
        dest_names = {d.name for _, d in pairs}
        self.assertIn("20260615_etf_daily.md", dest_names)
        self.assertIn("daily_brief.md", dest_names)


if __name__ == "__main__":
    unittest.main()
