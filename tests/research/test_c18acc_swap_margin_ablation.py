"""Tests for c18acc_swap_margin_ablation helpers."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_swap_margin_ablation import evaluate_vs_baseline
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


class TestSwapMarginAblation(unittest.TestCase):
    def test_evaluate_keep_baseline(self) -> None:
        slices = [
            {
                "variant_id": "M0",
                "label": "base",
                "full": {"mean_excess_pct": 5.0, "n_legs": 40},
                "is": {"mean_excess_pct": 5.0, "n_legs": 20},
                "oos": {"mean_excess_pct": 5.0, "n_legs": 20, "swaps": 10},
            },
            {
                "variant_id": "M4",
                "label": "none",
                "full": {"mean_excess_pct": 4.0, "n_legs": 40},
                "is": {"mean_excess_pct": 4.0, "n_legs": 20},
                "oos": {"mean_excess_pct": 4.0, "n_legs": 20, "swaps": 20},
            },
        ]
        d = evaluate_vs_baseline(slices)
        self.assertEqual(d["verdict"], "KEEP_SEG_LAST_MARGIN")

    def test_evaluate_go_simpler(self) -> None:
        slices = [
            {
                "variant_id": "M0",
                "label": "base",
                "full": {"mean_excess_pct": 5.0, "n_legs": 40},
                "is": {"mean_excess_pct": 5.0, "n_legs": 20},
                "oos": {"mean_excess_pct": 5.0, "n_legs": 20, "swaps": 10},
            },
            {
                "variant_id": "M1",
                "label": "m0",
                "full": {"mean_excess_pct": 5.5, "n_legs": 40},
                "is": {"mean_excess_pct": 5.2, "n_legs": 20},
                "oos": {"mean_excess_pct": 5.6, "n_legs": 20, "swaps": 12},
            },
        ]
        d = evaluate_vs_baseline(slices)
        self.assertEqual(d["verdict"], "GO_SIMPLER")
        self.assertTrue(d["rows"][1]["pass_strict"])

    def test_swap_margin_key_none_allows_weaker_seg(self) -> None:
        slots = [{"stock_id": "2330", "seg_last": 2.0, "entry_date": "2026-01-01"}]
        pool = [_row("3008", 1.0)]  # weaker seg_last
        cfg = ScoreSwapCConfig(
            sort_key="avg_accel_decel",
            score_margin=0.05,
            accel_sell_negative_only=False,
            buy_sort_key="avg_accel_decel",
            swap_margin_key="none",
        )
        sell, buy = _pick_swap_pair(
            slots,
            pool,
            held_ids={"2330"},
            config=cfg,
            held_today={"2330": -0.5},
            challenger_avg_accel={"3008": 0.3},
        )
        self.assertEqual(sell["stock_id"], "2330")
        self.assertEqual(buy.stock_id, "3008")

    def test_swap_margin_key_sort_key_requires_accel_beat(self) -> None:
        slots = [{"stock_id": "2330", "seg_last": 1.0, "entry_date": "2026-01-01"}]
        pool = [_row("3008", 9.0)]
        cfg = ScoreSwapCConfig(
            sort_key="avg_accel_decel",
            score_margin=0.0,
            accel_sell_negative_only=False,
            buy_sort_key="avg_accel_decel",
            swap_margin_key="sort_key",
        )
        sell, buy = _pick_swap_pair(
            slots,
            pool,
            held_ids={"2330"},
            config=cfg,
            held_today={"2330": -0.1},
            challenger_avg_accel={"3008": -0.2},  # weaker accel
        )
        self.assertIsNone(sell)
        self.assertIsNone(buy)

    def test_swap_margin_key_mom_minus_ratio(self) -> None:
        """Δ gate: challenger Mom−Ratio must beat held entry Δ + margin."""
        slots = [
            {
                "stock_id": "2330",
                "seg_last": 9.0,  # high seg would win under seg_last gate
                "rs_ratio": 110.0,
                "rs_momentum": 101.0,  # Δ = -9
                "mom_minus_ratio": -9.0,
                "entry_date": "2026-01-01",
            }
        ]
        # High Δ challenger despite low seg_last
        cool = ScanRow(
            stock_id="3008",
            stock_name="3008",
            fresh=True,
            mono=True,
            seg_last=0.5,
            disp=1.0,
            segs=[1.0, 1.1, 0.5],
            quadrants=[],
            rs_ratio=100.2,
            rs_momentum=102.0,  # Δ = +1.8 > -9 + 0.05
            daily_pct=None,
        )
        hot = ScanRow(
            stock_id="2454",
            stock_name="2454",
            fresh=True,
            mono=True,
            seg_last=9.0,
            disp=1.0,
            segs=[1.0, 1.1, 9.0],
            quadrants=[],
            rs_ratio=120.0,
            rs_momentum=103.0,  # Δ = -17 still worse than held
            daily_pct=None,
        )
        cfg = ScoreSwapCConfig(
            sort_key="avg_accel_decel",
            score_margin=0.05,
            accel_sell_negative_only=False,
            buy_sort_key="avg_accel_decel",
            swap_margin_key="mom_minus_ratio",
        )
        sell, buy = _pick_swap_pair(
            slots,
            [hot, cool],
            held_ids={"2330"},
            config=cfg,
            held_today={"2330": -0.5},
            challenger_avg_accel={"3008": 0.4, "2454": 0.5},
        )
        self.assertEqual(sell["stock_id"], "2330")
        self.assertEqual(buy.stock_id, "3008")  # higher Δ wins pick among beats


if __name__ == "__main__":
    unittest.main()
