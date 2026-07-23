"""Tests for c18acc_swap_ctx_sell_quad_study helpers."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_swap_ctx_sell_quad_study import (
    PHASE1_VARIANTS,
    evaluate_vs_baseline,
)
from research.backtest.rrg_mono_score_swap_c import _sell_quad_gate_ok


class TestCtxSellQuad(unittest.TestCase):
    def test_variants_cover_both_families(self) -> None:
        ids = [v["id"] for v in PHASE1_VARIANTS]
        self.assertIn("C_on", ids)
        self.assertIn("Q_path", ids)
        self.assertIn("CQ", ids)

    def test_sell_quad_none_passthrough(self) -> None:
        import pandas as pd

        dates = ["2026-01-01", "2026-01-02"]
        rs = pd.DataFrame({"2330": [100.0, 101.0]}, index=dates)
        mom = pd.DataFrame({"2330": [100.0, 99.0]}, index=dates)
        self.assertTrue(
            _sell_quad_gate_ok(
                "none",
                rs_ratio=rs,
                rs_mom=mom,
                full_dates=dates,
                as_of="2026-01-02",
                stock_id="2330",
                entry_date="2026-01-01",
            )
        )

    def test_evaluate(self) -> None:
        slices = [
            {
                "variant_id": "S0",
                "label": "b",
                "full": {"mean_excess_pct": 5.0, "n_legs": 40},
                "is": {"mean_excess_pct": 5.0, "n_legs": 20},
                "oos": {"mean_excess_pct": 5.0, "n_legs": 20},
            },
            {
                "variant_id": "C_hot",
                "label": "c",
                "full": {"mean_excess_pct": 4.0, "n_legs": 40},
                "is": {"mean_excess_pct": 4.0, "n_legs": 20},
                "oos": {"mean_excess_pct": 4.0, "n_legs": 20},
            },
        ]
        self.assertEqual(evaluate_vs_baseline(slices)["verdict"], "KEEP_CHAMPION")


if __name__ == "__main__":
    unittest.main()
