"""Tests for standalone expert intraday backtest."""

from __future__ import annotations

import sqlite3
import unittest

from research.backtest.expert_intraday_standalone import (
    _max_drawdown_pct,
    _stop_hit_intraday,
    detect_orb_entry,
    detect_standalone_entry,
)
from research.backtest.rrg_mono_expert_entry import detect_vwap_reclaim_entry
from stock_db.kbar import KbarBar


def _bar(
    minute: str,
    o: float,
    h: float,
    lo: float,
    c: float,
    vol: int = 1000,
) -> KbarBar:
    return KbarBar(minute=minute, open=o, high=h, low=lo, close=c, volume=vol)


def _orb_session() -> tuple[KbarBar, ...]:
  """Build OR 09:05-09:15 range 100-101, then breakout at 09:16."""
  bars: list[KbarBar] = []
  for i in range(5, 16):
    mm = f"09:{i:02d}:00"
    bars.append(_bar(mm, 100.2, 101.0, 100.0, 100.5, vol=500))
  bars.append(_bar("09:16:00", 100.8, 102.0, 100.7, 101.5, vol=2000))
  return tuple(bars)


class TestOrbEntry(unittest.TestCase):
    def test_orb_breakout_after_range(self) -> None:
        trig = detect_orb_entry(_orb_session())
        self.assertIsNotNone(trig)
        assert trig is not None
        self.assertEqual(trig.entry_minute, "09:16:00")
        self.assertAlmostEqual(trig.entry_px, 101.5)
        self.assertAlmostEqual(trig.stop_px, 100.0)

    def test_orb_skips_pre_0905(self) -> None:
        bars = (_bar("09:04:00", 100, 105, 99, 104, vol=5000),)
        self.assertIsNone(detect_orb_entry(bars))

    def test_standalone_routes_modes(self) -> None:
        bars = _orb_session()
        self.assertIsNotNone(detect_standalone_entry("orb", bars))
        reclaim_bars = tuple(
            [_bar(f"09:{i:02d}:00", 100, 100.2, 99.9, 100) for i in range(5, 10)]
            + [_bar("09:10:00", 100, 100.1, 99.5, 99.6), _bar("09:11:00", 99.6, 100.3, 99.4, 100.2)]
        )
        self.assertIsNotNone(detect_standalone_entry("vwap_reclaim", reclaim_bars))


class TestStopAndDrawdown(unittest.TestCase):
    def test_stop_hit_after_entry(self) -> None:
        bars = (
            _bar("09:10:00", 100, 100.5, 99.8, 100.2),
            _bar("09:11:00", 100.2, 100.3, 98.5, 99.0),
        )
        self.assertTrue(_stop_hit_intraday(bars, "09:10:00", 99.0))
        self.assertFalse(_stop_hit_intraday(bars, "09:10:00", 98.0))

    def test_max_drawdown(self) -> None:
        periods = [
            {"entry_date": "2024-01-02", "stock_id": "2330", "return_pct": 10.0},
            {"entry_date": "2024-01-02", "stock_id": "2317", "return_pct": -5.0},
            {"entry_date": "2024-01-03", "stock_id": "2454", "return_pct": -15.0},
        ]
        dd = _max_drawdown_pct(periods)
        self.assertIsNotNone(dd)
        assert dd is not None
        self.assertGreater(dd, 0)
        self.assertLess(dd, 20.0)


class TestVwapReclaimStillWorks(unittest.TestCase):
    def test_reclaim_via_detector(self) -> None:
        bars = tuple(
            [_bar(f"09:{i:02d}:00", 100, 100.2, 99.9, 100) for i in range(5, 8)]
            + [_bar("09:08:00", 100, 100.1, 99.5, 99.6), _bar("09:09:00", 99.6, 100.2, 99.4, 100.1)]
        )
        self.assertIsNotNone(detect_vwap_reclaim_entry(bars))


if __name__ == "__main__":
    unittest.main()
