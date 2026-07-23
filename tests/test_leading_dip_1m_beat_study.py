"""Unit tests for Leading Dip 1m beat helpers (no DB / no network)."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.leading_dip_1m_beat_study import (
    attach_lookback_excess,
    confirm_hit_index,
    resolve_entry_index,
    wait_deepen_hit_index,
)


class TestConfirmAndWait(unittest.TestCase):
    def test_confirm_needs_consecutive(self) -> None:
        mask = pd.Series([False, True, False, True, True, True, False])
        self.assertEqual(confirm_hit_index(mask, confirm_bars=3), 5)
        self.assertEqual(confirm_hit_index(mask, confirm_bars=2), 4)
        self.assertIsNone(confirm_hit_index(pd.Series([True, False, True]), confirm_bars=2))

    def test_wait_deepen_picks_local_min(self) -> None:
        mask = pd.Series([False, True, True, True, False])
        ex = pd.Series([0.0, -4.1, -5.5, -4.8, -3.0])
        minutes = pd.Series(["09:00", "09:10", "09:12", "09:14", "09:20"])
        i = wait_deepen_hit_index(mask, ex, minutes, wait_minutes=5)
        self.assertEqual(i, 2)  # -5.5 within 09:10..09:15

    def test_resolve_first_hit(self) -> None:
        f = pd.DataFrame(
            {
                "minute": ["09:05", "09:06", "09:07"],
                "gap": [-0.5, -0.9, -1.0],
                "ex": [-3.0, -4.2, -4.5],
            }
        )
        self.assertEqual(
            resolve_entry_index(
                f,
                mode="first_hit",
                confirm_bars=1,
                wait_minutes=0,
                gap3_max=-0.80,
                excess_max=-4.0,
            ),
            1,
        )

    def test_lookback_excess_vs_prior_bars(self) -> None:
        f = pd.DataFrame(
            {
                "minute": ["09:00", "09:01", "09:02", "09:03"],
                "S": [100.0, 99.0, 97.0, 96.0],
                "B": [100.0, 100.0, 100.0, 100.0],
                "gap": [0.0, 0.0, 0.0, 0.0],
            }
        )
        out = attach_lookback_excess(f, 2)
        # at 09:02 vs 09:00: S -3%, B 0% → ex_lb = -3
        self.assertAlmostEqual(float(out.loc[2, "ex_lb"]), -3.0, places=6)
        i = resolve_entry_index(
            out,
            mode="first_hit",
            confirm_bars=1,
            wait_minutes=0,
            gap3_max=-0.80,
            excess_max=-3.0,
            ex_col="ex_lb",
            require_gap=False,
        )
        self.assertEqual(i, 2)


if __name__ == "__main__":
    unittest.main()
