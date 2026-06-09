"""etf_signal_performance：ETF 加碼 H+20 勝率。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from etf_signal_performance import build_etf_signal_performance
from project_config import FLOW_VERSION
from stock_db import connect, upsert_flow_events, upsert_stock_daily_bars

SYNCED = "2026-06-01T00:00:00+00:00"


class TestEtfSignalPerformance(unittest.TestCase):
    def test_win_rate_by_etf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            upsert_flow_events(
                conn,
                [
                    {
                        "event_date": "2026-05-01",
                        "prev_date": "2026-04-28",
                        "stock_id": "2330",
                        "stock_name": "台積電",
                        "net_side": "add",
                        "consensus": "SINGLE",
                        "intent": "ROTATION_PLAY",
                        "conviction": 1.0,
                        "implied_flow_ntd": 1e8,
                        "etf_count": 1,
                        "source_etfs": "00981A",
                        "flow_version": FLOW_VERSION,
                    },
                    {
                        "event_date": "2026-05-08",
                        "prev_date": "2026-05-01",
                        "stock_id": "2330",
                        "stock_name": "台積電",
                        "net_side": "add",
                        "consensus": "SINGLE",
                        "intent": "ROTATION_PLAY",
                        "conviction": 1.0,
                        "implied_flow_ntd": 1e8,
                        "etf_count": 1,
                        "source_etfs": "00981A",
                        "flow_version": FLOW_VERSION,
                    },
                    {
                        "event_date": "2026-05-01",
                        "prev_date": "2026-04-28",
                        "stock_id": "2454",
                        "stock_name": "聯發科",
                        "net_side": "add",
                        "consensus": "SINGLE",
                        "intent": "ROTATION_PLAY",
                        "conviction": 1.0,
                        "implied_flow_ntd": 5e7,
                        "etf_count": 1,
                        "source_etfs": "00981A",
                        "flow_version": FLOW_VERSION,
                    },
                ],
            )
            bars = []
            for i, d in enumerate(
                [
                    "2026-05-01",
                    "2026-05-02",
                    "2026-05-08",
                    "2026-05-09",
                ]
            ):
                bars.append(
                    {
                        "stock_id": "2330",
                        "trade_date": d,
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.0 + i,
                        "volume": 1000,
                        "source": "finmind",
                    }
                )
            bars.append(
                {
                    "stock_id": "2454",
                    "trade_date": "2026-05-01",
                    "open": 50.0,
                    "high": 51.0,
                    "low": 49.0,
                    "close": 50.0,
                    "volume": 1000,
                    "source": "finmind",
                }
            )
            bars.append(
                {
                    "stock_id": "2454",
                    "trade_date": "2026-05-02",
                    "open": 48.0,
                    "high": 49.0,
                    "low": 47.0,
                    "close": 48.0,
                    "volume": 1000,
                    "source": "finmind",
                }
            )
            upsert_stock_daily_bars(conn, bars)
            for d in ("2026-05-01", "2026-05-02", "2026-05-08", "2026-05-09"):
                conn.execute(
                    """
                    INSERT INTO daily_bars (code, date, open, high, low, close, volume, source, synced_at)
                    VALUES ('IX0001', ?, 100, 101, 99, 100, 1000, 'tej', ?)
                    ON CONFLICT(code, date, source) DO UPDATE SET close=excluded.close
                    """,
                    (d, SYNCED),
                )
            conn.commit()
            rows = build_etf_signal_performance(
                conn, ("00981A",), horizon_days=1
            )
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.etf_code, "00981A")
            self.assertGreaterEqual(row.sample_n, 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
