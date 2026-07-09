"""Tests for RRG × Regime four-axis Phase 2."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.archive.rrg_regime_four_axis_phase2 import (
    _delta_pp,
    _evaluate_event_overlay,
    _metrics_with_excess,
    phase2_overlay_specs,
)


class TestRrgRegimeFourAxisPhase2(unittest.TestCase):
    def test_delta_pp(self) -> None:
        self.assertEqual(_delta_pp(1.0, 0.5), 0.5)
        self.assertIsNone(_delta_pp(None, 1.0))

    def test_metrics_with_excess(self) -> None:
        df = pd.DataFrame(
            {
                "date": ["2024-06-03", "2024-06-04"],
                "nxt_ret": [1.0, -0.5],
                "bench_nxt_ret": [0.2, -0.1],
                "alpha_nxt": [0.8, -0.4],
            }
        )
        m = _metrics_with_excess(df)
        self.assertEqual(m["mean_excess_nxt_pct"], 0.2)

    def test_s3_overlay_improves_vs_r0(self) -> None:
        df = pd.DataFrame(
            {
                "date": ["2024-06-03", "2024-06-04", "2026-06-03", "2026-06-04"],
                "s3_burst": [True, True, True, True],
                "bench_b0": [True, False, True, False],
                "nxt_ret": [1.0, -1.0, 0.8, -0.6],
                "bench_nxt_ret": [0.0, 0.0, 0.0, 0.0],
                "alpha_nxt": [1.0, -1.0, 0.8, -0.6],
            }
        )
        spec = next(s for s in phase2_overlay_specs() if s["overlay_id"] == "P2-S3-B0")
        row = _evaluate_event_overlay(
            df,
            universe_mask=df["s3_burst"],
            predicate=spec["predicate"],
            spec=spec,
        )
        self.assertEqual(row["FULL"]["n_events"], 2)
        self.assertGreater(row["FULL"]["mean_nxt_ret_pct"], 0.0)
        self.assertGreater(row["FULL"]["delta_nxt_vs_r0_pp"], 0.0)


if __name__ == "__main__":
    unittest.main()
