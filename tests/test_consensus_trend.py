"""consensus_trend：跨 ETF 加碼檔數時間序列。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from consensus_trend import (
    build_consensus_trend,
    consensus_trend_label,
    ConsensusTrendPoint,
)
from stock_db import connect, upsert_etf_holdings, upsert_etf_holdings_meta


def _seed_holdings(conn, etf: str, snap: str, stock: str, shares: float) -> None:
    upsert_etf_holdings_meta(
        conn,
        {
            "etf_code": etf,
            "snapshot_date": snap,
            "nav": 100.0,
            "holding_count": 1,
            "source": "test",
            "source_edit_at": None,
            "synced_at": "2026-01-01T00:00:00Z",
        },
    )
    upsert_etf_holdings(
        conn,
        [
            {
                "etf_code": etf,
                "snapshot_date": snap,
                "stock_id": stock,
                "stock_name": "測試",
                "shares": shares,
                "weight_pct": 1.0,
                "amount": None,
                "source": "test",
                "source_edit_at": None,
                "synced_at": "2026-01-01T00:00:00Z",
            }
        ],
    )


class TestConsensusTrend(unittest.TestCase):
    def test_declining_label(self) -> None:
        pts = [
            ConsensusTrendPoint("2026-05-01", 5),
            ConsensusTrendPoint("2026-05-08", 2),
        ]
        self.assertEqual(consensus_trend_label(pts), "衰退")

    def test_build_trend_from_holdings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            _seed_holdings(conn, "00981A", "2026-06-01", "2330", 1000)
            _seed_holdings(conn, "00981A", "2026-06-08", "2330", 1100)
            _seed_holdings(conn, "00982A", "2026-06-01", "2330", 500)
            _seed_holdings(conn, "00982A", "2026-06-08", "2330", 600)
            pts = build_consensus_trend(
                conn, ("00981A", "00982A"), "2330", max_points=4
            )
            self.assertGreaterEqual(len(pts), 1)
            self.assertEqual(pts[-1].etf_add_count, 2)
            conn.close()


if __name__ == "__main__":
    unittest.main()
