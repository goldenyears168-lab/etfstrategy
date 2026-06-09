"""sync_morning_futures：早盤 TX/TE 即時 gap。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stock_db import connect, load_execution_tx_gap, upsert_morning_risk_snapshot
from sync_morning_futures import (
    build_morning_risk_row,
    format_morning_risk_line,
    gap_pct,
    morning_radar_warnings,
    pick_snapshot_row,
)


class TestSyncMorningFutures(unittest.TestCase):
    def test_gap_pct(self) -> None:
        self.assertAlmostEqual(gap_pct(10100.0, 10000.0), 1.0)
        self.assertIsNone(gap_pct(None, 10000.0))

    def test_pick_snapshot_row(self) -> None:
        rows = [
            {"futures_id": "TXF", "close": 22500, "volume": 100},
            {"futures_id": "EXF", "close": 1180, "volume": 50},
        ]
        tx = pick_snapshot_row(rows, "TXF")
        self.assertIsNotNone(tx)
        assert tx is not None
        self.assertEqual(float(tx["close"]), 22500.0)

    def test_build_morning_risk_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            conn.execute(
                """
                INSERT INTO daily_bars
                (code, date, close, source, synced_at)
                VALUES ('IX0001', '2026-06-05', 22000, 'tej', 'x')
                """
            )
            conn.execute(
                """
                INSERT INTO tech_risk_daily_snapshot (
                    session_date, semi_benchmark, tw_spot_code, source_us, source_tw,
                    synced_at, te_futures_price
                ) VALUES (
                    '2026-06-05', 'SOX', 'IX0001', 'yahoo', 'finmind', 'x', 1150
                )
                """
            )
            conn.commit()
            row = build_morning_risk_row(
                conn,
                trade_date="2026-06-08",
                captured_at="2026-06-08T08:30:00+08:00",
                snapshot_rows=[
                    {"futures_id": "TXF", "close": 22100, "contract_date": "202606"},
                    {"futures_id": "EXF", "close": 1160, "contract_date": "202606"},
                ],
                tx_id="TXF",
                te_id="EXF",
            )
            conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertAlmostEqual(row["tx_gap_live_pct"], 0.4545, places=3)
        self.assertAlmostEqual(row["te_gap_live_pct"], 0.8696, places=3)
        self.assertAlmostEqual(row["te_minus_tx_pct"], 0.4151, places=3)

    def test_morning_radar_warnings(self) -> None:
        row = {
            "tx_gap_live_pct": 1.2,
            "te_minus_tx_pct": 0.5,
        }
        warns = morning_radar_warnings(row)
        self.assertEqual(len(warns), 2)

    def test_load_execution_tx_gap_prefers_morning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            upsert_morning_risk_snapshot(
                conn,
                {
                    "trade_date": "2026-06-08",
                    "captured_at": "2026-06-08T08:30:00+08:00",
                    "tw_spot_date": "2026-06-05",
                    "tw_spot_code": "IX0001",
                    "tw_spot_prev_close": 22000.0,
                    "tx_snapshot_id": "TXF",
                    "tx_price": 22200.0,
                    "tx_contract_date": "202606",
                    "tx_gap_live_pct": 0.9,
                    "te_snapshot_id": "EXF",
                    "te_price": 1150.0,
                    "te_contract_date": "202606",
                    "te_gap_live_pct": 0.5,
                    "te_minus_tx_pct": -0.4,
                    "source": "test",
                    "notes": None,
                },
            )
            gap, src = load_execution_tx_gap(conn, trade_date="2026-06-08")
            conn.close()
        self.assertAlmostEqual(gap, 0.9)
        self.assertEqual(src, "morning_live")

    def test_format_morning_risk_line(self) -> None:
        line = format_morning_risk_line(
            {
                "trade_date": "2026-06-08",
                "captured_at": "08:30",
                "tx_gap_live_pct": 0.4,
                "tx_price": 22100,
                "te_gap_live_pct": 0.7,
                "te_price": 1160,
                "te_minus_tx_pct": 0.3,
            }
        )
        self.assertIn("TX gap +0.40%", line)
        self.assertIn("TE-TX +0.30%", line)


if __name__ == "__main__":
    unittest.main()
