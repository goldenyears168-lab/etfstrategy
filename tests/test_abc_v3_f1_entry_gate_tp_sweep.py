"""Tests for abc_v3_f1_entry_gate_tp_sweep."""

from __future__ import annotations

import unittest

from research.backtest.abc_v3_f1_entry_gate_tp_sweep import (
    _cell_matches_leg,
    default_joint_sweep_grid,
)
from research.backtest.abc_v3_f1_entry_structure_sweep import EntryStructureParams, GapSweetBand


def _cell(**overrides) -> EntryStructureParams:
    base = dict(
        id="test",
        label="test",
        gap_band=GapSweetBand(
            gap_w20_w5_min=2.0, gap_w20_w5_max=7.0, gap_w5_w3_min=-0.5, gap_w5_w3_max=1.5
        ),
        rebound_min=0.3,
        rebound_mode="rebound_only",
        min_poll_idx=1,
    )
    base.update(overrides)
    return EntryStructureParams(**base)


def _leg(**overrides) -> dict:
    base = dict(
        gate_poll_idx=2,
        gate_gap_w20_w5=4.0,
        gate_gap_w5_w3=0.5,
        gate_rv3_rebound=0.5,
        gate_rv3_poll_rise=True,
    )
    base.update(overrides)
    return base


class TestCellMatchesLeg(unittest.TestCase):
    def test_matching_leg_passes(self) -> None:
        self.assertTrue(_cell_matches_leg(_cell(), _leg()))

    def test_poll_idx_below_min_fails(self) -> None:
        self.assertFalse(_cell_matches_leg(_cell(), _leg(gate_poll_idx=0)))

    def test_gap_outside_band_fails(self) -> None:
        self.assertFalse(_cell_matches_leg(_cell(), _leg(gate_gap_w20_w5=10.0)))
        self.assertFalse(_cell_matches_leg(_cell(), _leg(gate_gap_w5_w3=-2.0)))

    def test_rebound_below_min_fails(self) -> None:
        self.assertFalse(_cell_matches_leg(_cell(), _leg(gate_rv3_rebound=0.1)))

    def test_rebound_and_poll_rise_mode_requires_poll_rise(self) -> None:
        cell = _cell(rebound_mode="rebound_and_poll_rise")
        self.assertFalse(_cell_matches_leg(cell, _leg(gate_rv3_poll_rise=False)))
        self.assertTrue(_cell_matches_leg(cell, _leg(gate_rv3_poll_rise=True)))

    def test_missing_gate_fields_fail_closed(self) -> None:
        self.assertFalse(_cell_matches_leg(_cell(), _leg(gate_gap_w20_w5=None)))
        self.assertFalse(_cell_matches_leg(_cell(), _leg(gate_rv3_rebound=None)))


class TestDefaultJointSweepGrid(unittest.TestCase):
    def test_grid_is_nonempty_and_ids_unique(self) -> None:
        grid = default_joint_sweep_grid()
        self.assertEqual(len(grid), 60)
        ids = {c.id for c in grid}
        self.assertEqual(len(ids), len(grid))


if __name__ == "__main__":
    unittest.main()
