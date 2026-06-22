"""Tests for stock_daily_lens · delta · convergence · narrative."""

from __future__ import annotations

import sqlite3
import unittest

from stock_daily_lens import (
    LensRow,
    _compute_convergence,
    _regime_aligned_for_stock,
    build_narrative_zh,
    build_stock_daily_lens_rows,
)
from stock_db import upsert_stock_daily_lens_rows
from stock_db._schema import _SCHEMA


class StockDailyLensTests(unittest.TestCase):
    def test_regime_aligned_same_sign(self) -> None:
        self.assertTrue(_regime_aligned_for_stock(5.0, 2.0))
        self.assertTrue(_regime_aligned_for_stock(-3.0, -1.0))
        self.assertFalse(_regime_aligned_for_stock(5.0, -1.0))
        self.assertFalse(_regime_aligned_for_stock(None, 1.0))

    def test_signal_convergence_four_frameworks(self) -> None:
        row = LensRow(
            trade_date="2026-06-22",
            stock_id="2449",
            stock_name="京元電子",
            consensus_add=True,
            regime_aligned=True,
            rrg_quadrant="leading",
            rrg_mono_fresh=True,
            vcp_composite=50.0,
            vcp_execution_state="Pre-breakout",
        )
        self.assertEqual(_compute_convergence(row), 4)

    def test_narrative_includes_delta_prefix_and_convergence(self) -> None:
        row = LensRow(
            trade_date="2026-06-22",
            stock_id="2449",
            stock_name="京元電子",
            consensus_add=True,
            etf_add_codes=["00981A", "00982A"],
            delta_consensus_new_today=True,
            rrg_quadrant="leading",
            vcp_distance_pivot_pct=2.3,
            vcp_composite=52.0,
            signal_convergence=3,
        )
        text = build_narrative_zh(row)
        self.assertIn("【今日首次共識】", text)
        self.assertIn("2449", text)
        self.assertIn("四框架收斂 3/4", text)

    def test_narrative_includes_new_observation_prefix(self) -> None:
        row = LensRow(
            trade_date="2026-06-22",
            stock_id="2330",
            stock_name="台積電",
            delta_new_to_watchlist=True,
        )
        text = build_narrative_zh(row)
        self.assertIn("【新進觀察】", text)

    def test_prev_pool_sqlite(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        upsert_stock_daily_lens_rows(
            conn,
            [
                {
                    "trade_date": "2026-06-20",
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "etf_add_count": 1,
                    "etf_reduce_count": 0,
                    "etf_add_codes": ["00981A"],
                    "etf_flow_ntd": None,
                    "share_delta_total": None,
                    "growth_pct": None,
                    "consensus_add": False,
                    "consensus_streak_days": 0,
                    "regime_aligned": False,
                    "rrg_mono_fresh": False,
                    "rrg_tier2": False,
                    "copytrade_l1h9_signal": False,
                    "delta_new_to_watchlist": False,
                    "delta_consensus_new_today": False,
                    "delta_any_signal": False,
                    "signal_convergence": 0,
                    "lens_score": 10.0,
                    "narrative_zh": "old",
                    "highlight_tier": "none",
                    "holdings_aligned": True,
                    "data_baseline_date": "2026-06-20",
                    "sources_json": "{}",
                }
            ],
        )
        prev = conn.execute(
            "SELECT stock_id FROM stock_daily_lens WHERE trade_date = ?",
            ("2026-06-20",),
        ).fetchall()
        self.assertEqual({r[0] for r in prev}, {"2330"})
        conn.close()

    def test_build_rows_empty_pool(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.executescript(
            """
            INSERT INTO daily_bars (code, date, source, open, high, low, close, volume, synced_at)
            VALUES ('IX0001', '2026-06-20', 'tej', 1, 1, 1, 1, 1, '2026-06-20T00:00:00Z');
            """
        )
        conn.commit()
        rows = build_stock_daily_lens_rows(conn, "2026-06-20")
        self.assertEqual(rows, [])
        conn.close()


if __name__ == "__main__":
    unittest.main()
