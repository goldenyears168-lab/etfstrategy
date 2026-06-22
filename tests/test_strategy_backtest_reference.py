"""strategy_backtest_reference · Supabase backtest_reference payload."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research.backtest.slot_backtest_summary import (
    SlotBacktestConfig,
    build_summary_payload,
    write_slot_backtest_summary,
)
from strategy_backtest_reference import build_backtest_reference


class TestStrategyBacktestReference(unittest.TestCase):
    def test_loads_from_summary_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "rrg_mono_hold7_slot_backtest_2026.json"
            cfg = SlotBacktestConfig(
                date_start="2026-01-01",
                date_end="2026-06-30",
                n_slots=3,
                hold_days=7,
                source_summary=str(summary_path),
            )
            payload = build_summary_payload(
                track_id="rrg-mono-hold7",
                config=cfg,
                summary={
                    "n_periods": 39,
                    "win_rate_vs_bench_pct": 58.97,
                    "mean_excess_pct": 7.0,
                    "mean_return_pct": 14.2,
                },
                source_module="rrg_mono_backtest",
            )
            write_slot_backtest_summary(summary_path, payload)

            import strategy_config as sc

            orig = sc.DEFAULT_CONFIG
            fake_yaml = Path(tmp) / "strategy.yaml"
            fake_yaml.write_text(
                f"""
version: strategy-v1
layer: strategy
benchmark_default: IX0001
principles: []
strategies:
  rrg-mono-hold7:
    title: RRG mono
    kind: competition
    schedule: launchd
    enabled: false
    n_slots: 3
    hold_days: 7
    backtest:
      spec_type: slot_strategy_backtest
      metrics:
        - n_periods
        - win_rate_vs_bench_pct
        - mean_excess_pct
        - mean_return_pct
      date_start: "2026-01-01"
      date_end: "2026-06-30"
      source_summary: "{summary_path}"
""",
                encoding="utf-8",
            )
            sc.DEFAULT_CONFIG = fake_yaml
            try:
                ref = build_backtest_reference("rrg-mono-hold7")
            finally:
                sc.DEFAULT_CONFIG = orig

            self.assertIsNotNone(ref)
            assert ref is not None
            self.assertEqual(ref["n_periods"], 39)
            self.assertAlmostEqual(ref["win_rate_vs_bench_pct"], 58.97)
            self.assertAlmostEqual(ref["expected_excess_pct"], 7.0)
            self.assertAlmostEqual(ref["historical_win_rate_vs_bench_pct"], 58.97)
            self.assertNotIn("disclaimer_zh", ref)


if __name__ == "__main__":
    unittest.main()
