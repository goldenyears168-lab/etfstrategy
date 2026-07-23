"""Tests for c18acc_swap_sell_gates_study helpers."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_swap_sell_gates_study import (
    build_phase1_variants,
    evaluate_vs_baseline,
)
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    _avg_accel_decline_streak_days,
    _pick_swap_pair,
)
from rrg_mono_daily_brief import ScanRow
import pandas as pd


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


class TestSellGates(unittest.TestCase):
    def test_build_variants(self) -> None:
        specs = build_phase1_variants({"gap_p25": 0.05, "gap_p50": 0.1})
        self.assertEqual([s["id"] for s in specs], ["S0", "J2", "J2o", "W25", "W50", "JW"])

    def test_rel_weak_gap_blocks_close_scores(self) -> None:
        slots = [
            {"stock_id": "2330", "seg_last": 1.0, "entry_date": "2026-01-01"},
            {"stock_id": "2454", "seg_last": 1.1, "entry_date": "2026-01-01"},
        ]
        pool = [_row("3008", 2.0)]
        cfg = ScoreSwapCConfig(
            sort_key="avg_accel_decel",
            score_margin=0.0,
            accel_sell_negative_only=False,
            buy_sort_key="avg_accel_decel",
            sell_rel_weak_gap=0.5,
        )
        sell, buy = _pick_swap_pair(
            slots,
            pool,
            held_ids={"2330", "2454"},
            config=cfg,
            held_today={"2330": -0.4, "2454": -0.35},  # gap 0.05 < 0.5
            challenger_avg_accel={"3008": 0.2},
        )
        self.assertIsNone(sell)
        self.assertIsNone(buy)

    def test_decline_streak_helper(self) -> None:
        dates = [f"2026-01-{d:02d}" for d in range(1, 12)]
        # Rising then falling momentum path → recent decline
        rs_ratio = pd.DataFrame(
            {"2330": [100 + i * 0.5 for i in range(8)] + [104, 103.5, 102.5]},
            index=dates,
        )
        rs_mom = pd.DataFrame(
            {"2330": [100 + i * 0.8 for i in range(8)] + [106, 105, 103]},
            index=dates,
        )
        streak = _avg_accel_decline_streak_days(
            rs_ratio,
            rs_mom,
            dates,
            as_of=dates[-1],
            stock_id="2330",
            lb=4,
        )
        self.assertGreaterEqual(streak, 1)

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
                "variant_id": "J2",
                "label": "j",
                "full": {"mean_excess_pct": 4.0, "n_legs": 40},
                "is": {"mean_excess_pct": 4.0, "n_legs": 20},
                "oos": {"mean_excess_pct": 4.0, "n_legs": 20},
            },
        ]
        self.assertEqual(evaluate_vs_baseline(slices)["verdict"], "KEEP_CHAMPION_SELL")


if __name__ == "__main__":
    unittest.main()
