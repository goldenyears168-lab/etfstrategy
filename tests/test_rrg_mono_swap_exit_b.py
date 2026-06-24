"""Tests for RRG mono swap exit mode B."""

from __future__ import annotations

import unittest

from research.backtest.rrg_mono_swap_exit_b import (
    SwapExitBConfig,
    _best_challenger,
    _passes_structural_gate,
)
from rrg_mono_daily_brief import ScanRow


def _row(sid: str, seg: float) -> ScanRow:
    return ScanRow(
        stock_id=sid,
        stock_name=sid,
        fresh=True,
        mono=True,
        seg_last=seg,
        disp=1.2,
        segs=[],
        quadrants=[],
        rs_ratio=100.0,
        rs_momentum=100.0,
        daily_pct=None,
    )


class TestSwapExitB(unittest.TestCase):
    def test_structural_down_left(self) -> None:
        cfg = SwapExitBConfig()
        self.assertTrue(_passes_structural_gate({"trend": "down_left"}, config=cfg))
        self.assertFalse(_passes_structural_gate({"trend": "up_right"}, config=cfg))

    def test_challenger_beats_entry_seg(self) -> None:
        cfg = SwapExitBConfig(challenger_beat="entry_seg")
        pool = [_row("2330", 3.0), _row("2454", 1.5)]
        best = _best_challenger(
            pool,
            held_ids={"9999"},
            held_entry_seg=2.0,
            held_today_seg=1.0,
            config=cfg,
        )
        self.assertIsNotNone(best)
        assert best is not None
        self.assertEqual(best.stock_id, "2330")

    def test_no_challenger_when_all_weaker(self) -> None:
        cfg = SwapExitBConfig()
        pool = [_row("2330", 1.0)]
        self.assertIsNone(
            _best_challenger(
                pool,
                held_ids=set(),
                held_entry_seg=2.0,
                held_today_seg=1.5,
                config=cfg,
            )
        )


if __name__ == "__main__":
    unittest.main()
