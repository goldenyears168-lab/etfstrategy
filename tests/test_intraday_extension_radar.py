"""Tests for intraday extension radar · 3711 spike-fade case."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from intraday_extension_radar import (
    build_bar_snapshots,
    pick_exit_for_mode,
    scan_session,
)
from stock_db.kbar import KbarBar, load_kbar_day_bars


ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "stocks.db"


class TestIntradayExtensionRadar(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not DB.is_file():
            raise unittest.SkipTest("stocks.db missing")
        cls.conn = sqlite3.connect(DB)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def test_3711_jun26_climax_and_signals(self) -> None:
        bars = load_kbar_day_bars(self.conn, "3711", "2026-06-26")
        if len(bars) < 50:
            self.skipTest("3711 2026-06-26 kbar insufficient")
        prev_close = 641.0
        ma20 = 601.0
        scan = scan_session(
            bars,
            trade_date="2026-06-26",
            stock_id="3711",
            prev_close=prev_close,
            ma20=ma20,
        )
        peak = max(scan.bars, key=lambda b: b.close)
        self.assertGreaterEqual(peak.close, 690.0)
        self.assertGreaterEqual(peak.ext_prev_pct, 7.0)
        self.assertTrue(peak.is_climax_bar or peak.heat_score >= 70)
        self.assertIsNotNone(scan.first_yellow)
        self.assertIsNotNone(scan.heat_exit)
        combo = pick_exit_for_mode(scan, "combo_b")
        self.assertIsNotNone(combo)
        self.assertLess(combo.price, peak.close)

    def test_zone_green_on_small_move(self) -> None:
        bars = (
            KbarBar("09:05:00", 100.0, 100.5, 99.5, 100.2, 1000),
            KbarBar("09:06:00", 100.2, 100.4, 100.0, 100.3, 900),
        )
        snaps = build_bar_snapshots(bars, prev_close=100.0, ma20=98.0)
        self.assertEqual(snaps[-1].zone, "green")


if __name__ == "__main__":
    unittest.main()
