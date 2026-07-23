"""Tests for c18acc_swap_rel_accel_confirm_study helpers."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_swap_rel_accel_confirm_study import (
    build_phase1_variants,
    evaluate_vs_baseline,
)
from research.backtest.rrg_mono_score_swap_c import ScoreSwapCConfig, _pick_swap_pair
from rrg_mono_daily_brief import ScanRow


def _row(stock_id: str, seg_last: float) -> ScanRow:
    return ScanRow(
        stock_id=stock_id,
        stock_name=stock_id,
        fresh=True,
        mono=True,
        seg_last=seg_last,
        disp=1.0,
        segs=[1.0, 1.1, seg_last],
        quadrants=[],
        rs_ratio=100.0,
        rs_momentum=100.0,
        daily_pct=None,
    )


class TestRelAccelConfirm(unittest.TestCase):
    def test_build_variants(self) -> None:
        specs = build_phase1_variants({"delta_p25": 0.01, "delta_p50": 0.02})
        ids = [s["id"] for s in specs]
        self.assertEqual(ids, ["B0", "R25", "R50", "D25", "C2", "DC2"])
        self.assertEqual(specs[3]["swap_accel_margin"], 0.01)
        self.assertEqual(specs[4]["swap_confirm_days"], 2)

    def test_dual_accel_margin_filters(self) -> None:
        slots = [{"stock_id": "2330", "seg_last": 1.0, "entry_date": "2026-01-01"}]
        pool = [_row("3008", 2.0)]
        cfg = ScoreSwapCConfig(
            sort_key="avg_accel_decel",
            score_margin=0.05,
            accel_sell_negative_only=False,
            buy_sort_key="avg_accel_decel",
            swap_margin_key="seg_last",
            swap_accel_margin=0.2,
        )
        sell, buy = _pick_swap_pair(
            slots,
            pool,
            held_ids={"2330"},
            config=cfg,
            held_today={"2330": -0.5},
            challenger_avg_accel={"3008": -0.4},  # gap 0.1 < 0.2
        )
        self.assertIsNone(sell)
        self.assertIsNone(buy)

    def test_evaluate_keep(self) -> None:
        slices = [
            {
                "variant_id": "B0",
                "label": "b",
                "full": {"mean_excess_pct": 5.0, "n_legs": 40},
                "is": {"mean_excess_pct": 5.0, "n_legs": 20},
                "oos": {"mean_excess_pct": 5.0, "n_legs": 20},
            },
            {
                "variant_id": "R25",
                "label": "r",
                "full": {"mean_excess_pct": 4.0, "n_legs": 40},
                "is": {"mean_excess_pct": 4.0, "n_legs": 20},
                "oos": {"mean_excess_pct": 4.0, "n_legs": 20},
            },
        ]
        d = evaluate_vs_baseline(slices)
        self.assertEqual(d["verdict"], "KEEP_CHAMPION_SWAP")


if __name__ == "__main__":
    unittest.main()
