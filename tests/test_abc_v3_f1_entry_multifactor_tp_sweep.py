"""Tests for abc_v3_f1_entry_multifactor_tp_sweep filter matching."""

from __future__ import annotations

import unittest

from research.backtest.abc_v3_f1_entry_multifactor_tp_sweep import (
    MultifactorOverlay,
    _leg_entry_trade_dict,
    _leg_matches_overlay,
    _matches_base_gap_gate,
    default_multifactor_overlay_grid,
)


def _leg(**overrides) -> dict:
    base = dict(
        gate_poll_idx=2,
        gate_gap_w20_w5=4.0,
        gate_gap_w5_w3=0.5,
        entry_t0={
            "w3_rv": 99.8,
            "w3_mv": 100.5,
            "w5_mv": 100.2,
            "w20_rv": 104.0,
            "w3_quad": "improving",
            "spread_class": "pullback",
        },
        return_pct=5.0,
    )
    base.update(overrides)
    return base


class TestBaseGapGate(unittest.TestCase):
    def test_matching_leg_passes(self) -> None:
        self.assertTrue(_matches_base_gap_gate(_leg()))

    def test_poll_idx_zero_fails(self) -> None:
        self.assertFalse(_matches_base_gap_gate(_leg(gate_poll_idx=0)))

    def test_gap_outside_band_fails(self) -> None:
        self.assertFalse(_matches_base_gap_gate(_leg(gate_gap_w20_w5=10.0)))
        self.assertFalse(_matches_base_gap_gate(_leg(gate_gap_w5_w3=-2.0)))


class TestLegEntryTradeDict(unittest.TestCase):
    def test_maps_quadrant_and_spread(self) -> None:
        trade = _leg_entry_trade_dict(_leg())
        self.assertEqual(trade["w3_quadrant"], "improving")
        self.assertEqual(trade["spread_class"], "pullback")
        self.assertTrue(trade["w3_rv_shallow"])
        self.assertAlmostEqual(trade["w20_w3_rv_spread"], 4.2)


class TestOverlayMatching(unittest.TestCase):
    def test_w3_improving_passes(self) -> None:
        ov = MultifactorOverlay("t", "t", ("w3_improving",))
        self.assertTrue(_leg_matches_overlay(_leg(), ov))

    def test_w3_improving_fails_when_mv_low(self) -> None:
        ov = MultifactorOverlay("t", "t", ("w3_improving",))
        self.assertFalse(
            _leg_matches_overlay(_leg(entry_t0={**_leg()["entry_t0"], "w3_mv": 99.5}), ov)
        )

    def test_avoid_lagging_rejects_lagging(self) -> None:
        ov = MultifactorOverlay("t", "t", ("avoid_w3_lagging",))
        self.assertFalse(
            _leg_matches_overlay(_leg(entry_t0={**_leg()["entry_t0"], "w3_quad": "lagging"}), ov)
        )

    def test_w20_rv_lte_105_5(self) -> None:
        ov = MultifactorOverlay("t", "t", ("w20_rv_lte_105.5",))
        self.assertTrue(_leg_matches_overlay(_leg(), ov))
        self.assertFalse(
            _leg_matches_overlay(_leg(entry_t0={**_leg()["entry_t0"], "w20_rv": 106.0}), ov)
        )

    def test_combo_requires_all_filters(self) -> None:
        ov = MultifactorOverlay(
            "combo",
            "combo",
            ("w3_improving", "w20_rv_lte_105.5"),
        )
        self.assertTrue(_leg_matches_overlay(_leg(), ov))
        self.assertFalse(
            _leg_matches_overlay(_leg(entry_t0={**_leg()["entry_t0"], "w20_rv": 106.0}), ov)
        )

    def test_none_overlay_always_passes(self) -> None:
        ov = MultifactorOverlay("none", "none", ())
        self.assertTrue(_leg_matches_overlay(_leg(), ov))


class TestDefaultGrid(unittest.TestCase):
    def test_grid_nonempty_unique_ids(self) -> None:
        grid = default_multifactor_overlay_grid()
        self.assertGreater(len(grid), 5)
        ids = {c.id for c in grid}
        self.assertEqual(len(ids), len(grid))
        self.assertIn("none", ids)
        self.assertIn("w3_improving+avoid_lag+w20_rv105.5", ids)


if __name__ == "__main__":
    unittest.main()
