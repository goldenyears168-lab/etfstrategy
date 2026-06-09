"""stock_context / trade_levels 單元測試。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from market_labels import (
    CHIP_FOREIGN_SELL_DIV,
    CHIP_SYNC_BUY,
    VOL_DOWN,
    VOL_SURGE,
)
from stock_context import classify_chip_resonance, classify_volume, compute_technical
from stock_db import connect
from trade_levels import TradeLevel


class TestClassify(unittest.TestCase):
    def test_triple_resonance(self) -> None:
        tag, _ = classify_chip_resonance("ETF加碼", 100.0, 50.0)
        self.assertEqual(tag, CHIP_SYNC_BUY)

    def test_knife_catch(self) -> None:
        tag, _ = classify_chip_resonance("ETF加碼", -50_000_000, 0.0)
        self.assertEqual(tag, CHIP_FOREIGN_SELL_DIV)

    def test_volume_ratio(self) -> None:
        self.assertEqual(classify_volume(2.5), VOL_SURGE)
        self.assertEqual(classify_volume(0.5), VOL_DOWN)


class TestTechnical(unittest.TestCase):
    def test_sma_from_rows(self) -> None:
        rows = []
        for i in range(30):
            rows.append(
                {
                    "trade_date": f"2026-05-{i+1:02d}",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0 + i * 0.1,
                    "volume": 1000 + i * 10,
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            for r in rows:
                conn.execute(
                    """
                    INSERT INTO stock_daily_bars (
                        stock_id, trade_date, open, high, low, close, volume, source, synced_at
                    ) VALUES ('2330', ?, ?, ?, ?, ?, ?, 'finmind', 'x')
                    """,
                    (
                        r["trade_date"],
                        r["open"],
                        r["high"],
                        r["low"],
                        r["close"],
                        int(r["volume"]),
                    ),
                )
            conn.commit()
            tech = compute_technical(conn, "2330")
            self.assertIsNotNone(tech)
            assert tech is not None
            self.assertIsNotNone(tech.dist_ma20_pct)
            self.assertIsNotNone(tech.vol_ratio_5d)
            conn.close()


class TestTradeLevels(unittest.TestCase):
    def test_rr(self) -> None:
        lv = TradeLevel("2330", 1000, 970, 1100)
        self.assertTrue(lv.valid)
        self.assertAlmostEqual(lv.risk_pct, 3.0)
        self.assertAlmostEqual(lv.reward_pct, 10.0)
        self.assertAlmostEqual(lv.risk_reward, 10 / 3, places=1)


if __name__ == "__main__":
    unittest.main()
