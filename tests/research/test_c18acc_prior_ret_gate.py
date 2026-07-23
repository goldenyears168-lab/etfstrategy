"""Tests for G_R5_12 prior-return chase gate helpers."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    champion_score_swap_c_config,
    filter_shortlist_by_prior_ret,
    prior_n_day_return,
    prior_ret_gate_allows,
)
from rrg_mono_daily_brief import ScanRow


def _row(sid: str) -> ScanRow:
    return ScanRow(
        stock_id=sid,
        stock_name=sid,
        fresh=True,
        mono=True,
        seg_last=1.0,
        disp=1.2,
        segs=[0.1, 0.2, 1.0],
        quadrants=["leading"] * 4,
        rs_ratio=101.0,
        rs_momentum=101.0,
        daily_pct=1.0,
    )


class TestPriorRetGate(unittest.TestCase):
    def setUp(self) -> None:
        # 02..11 · as_of=11 → end=10 start=05
        # A: 100→120 over path ending day10 → large prior5d
        dates = [f"2026-01-{d:02d}" for d in range(2, 12)]
        closes_a = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0, 112.0, 114.0, 116.0, 120.0]
        closes_b = [50.0] * 10
        self.dates = dates
        self.close = pd.DataFrame({"A": closes_a, "B": closes_b}, index=pd.Index(dates))

    def test_champion_has_r5_12(self) -> None:
        cfg = champion_score_swap_c_config()
        self.assertEqual(cfg.entry_prior_ret_days, 5)
        self.assertEqual(cfg.entry_prior_ret_max, 0.12)

    def test_prior_return_and_gate(self) -> None:
        # end=01-10 (116) / start=01-05 (106) - 1 ≈ 9.43%
        ret = prior_n_day_return(
            self.close, "A", self.dates, as_of="2026-01-11", n_days=5
        )
        self.assertIsNotNone(ret)
        assert ret is not None
        self.assertAlmostEqual(ret, 116.0 / 106.0 - 1.0, places=6)
        self.assertFalse(
            prior_ret_gate_allows(
                self.close,
                "A",
                self.dates,
                as_of="2026-01-11",
                n_days=5,
                max_ret=0.08,
            )
        )
        self.assertTrue(
            prior_ret_gate_allows(
                self.close,
                "A",
                self.dates,
                as_of="2026-01-11",
                n_days=5,
                max_ret=0.12,
            )
        )
        self.assertTrue(
            prior_ret_gate_allows(
                self.close,
                "B",
                self.dates,
                as_of="2026-01-11",
                n_days=5,
                max_ret=0.12,
            )
        )

    def test_missing_history_allows(self) -> None:
        short_dates = self.dates[:3]
        self.assertTrue(
            prior_ret_gate_allows(
                self.close,
                "A",
                short_dates,
                as_of="2026-01-04",
                n_days=5,
                max_ret=0.12,
            )
        )

    def test_filter_shortlist(self) -> None:
        cfg = ScoreSwapCConfig(
            "T", "t", entry_prior_ret_days=5, entry_prior_ret_max=0.08
        )
        rows = [_row("A"), _row("B")]
        out = filter_shortlist_by_prior_ret(
            rows,
            close=self.close,
            full_dates=self.dates,
            as_of="2026-01-11",
            config=cfg,
        )
        self.assertEqual([r.stock_id for r in out], ["B"])


if __name__ == "__main__":
    unittest.main()
