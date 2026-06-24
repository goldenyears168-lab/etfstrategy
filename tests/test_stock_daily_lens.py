"""Tests for stock_daily_lens · delta · convergence · narrative."""

from __future__ import annotations

import sqlite3
import unittest

from stock_daily_lens import (
    LensRow,
    _apply_deltas,
    _apply_featured_ranks,
    _compute_convergence,
    _compute_rrg_universe_rank,
    _monitoring_pool,
    _regime_aligned_for_stock,
    build_badges_json,
    build_narrative_zh,
    build_stock_daily_lens_rows,
)
from lens_ui_copy import RRG_FRESH_ZH


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
            rrg_mono_fresh=True,
            vcp_distance_pivot_pct=2.3,
            vcp_composite=52.0,
            regime_aligned=True,
            signal_convergence=3,
        )
        text = build_narrative_zh(row)
        self.assertIn("【跨 ETF 共識加碼】", text)
        self.assertIn("2449", text)
        self.assertIn(f"RRG 領先 {RRG_FRESH_ZH}", text)
        self.assertIn("距突破價 2.3%", text)
        self.assertIn("大盤同向", text)
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

    def test_apply_deltas_marks_new_watchlist_member(self) -> None:
        row = LensRow(trade_date="2026-06-22", stock_id="2330", stock_name="台積電")
        _apply_deltas(row, None, in_prev_pool=False)
        self.assertTrue(row.delta_new_to_watchlist)
        self.assertTrue(row.delta_any_signal)

    def test_apply_deltas_tracks_consensus_streak(self) -> None:
        row = LensRow(
            trade_date="2026-06-22",
            stock_id="2330",
            consensus_add=True,
        )
        _apply_deltas(row, {"consensus_add": True, "consensus_streak_days": 3}, in_prev_pool=True)
        self.assertFalse(row.delta_new_to_watchlist)
        self.assertEqual(row.consensus_streak_days, 4)
        self.assertFalse(row.delta_consensus_new_today)

    def test_monitoring_pool_uses_constituent_universe(self) -> None:
        pool = _monitoring_pool(
            {"2330": "台積電", "2454": "聯發科"},
            {},
            {},
            {},
        )
        self.assertEqual(pool, {"2330", "2454"})

    def test_build_rows_empty_pool(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from stock_db._schema import _SCHEMA
        from unittest.mock import patch

        conn.executescript(_SCHEMA)
        conn.executescript(
            """
            INSERT INTO daily_bars (code, date, source, open, high, low, close, volume, synced_at)
            VALUES ('IX0001', '2026-06-20', 'tej', 1, 1, 1, 1, 1, '2026-06-20T00:00:00Z');
            """
        )
        conn.commit()
        with patch("stock_daily_lens._constituent_name_map", return_value={}):
            rows = build_stock_daily_lens_rows(conn, "2026-06-20", prev_highlight_rows=[])
        self.assertEqual(rows, [])
        conn.close()

    def test_compute_rrg_universe_rank_orders_by_rs_ratio(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        specs = [
            ("2330", 110.0, 102.0),
            ("2454", 108.0, 101.0),
            ("3008", 110.0, 103.0),
        ]
        rows = [
            conn.execute(
                "SELECT ? AS stock_id, ? AS rs_ratio, ? AS rs_momentum",
                (sid, rs, mom),
            ).fetchone()
            for sid, rs, mom in specs
        ]
        total, rank_map = _compute_rrg_universe_rank(rows)
        self.assertEqual(total, 3)
        self.assertEqual(rank_map["3008"]["rrg_rank"], 1)
        self.assertEqual(rank_map["2330"]["rrg_rank"], 2)
        self.assertEqual(rank_map["2454"]["rrg_rank"], 3)
        conn.close()

    def test_compute_rrg_universe_rank_skips_null_rs_ratio(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        rows = [
            conn.execute(
                "SELECT ? AS stock_id, ? AS rs_ratio, ? AS rs_momentum",
                ("2330", 105.0, 100.0),
            ).fetchone(),
            conn.execute(
                "SELECT ? AS stock_id, ? AS rs_ratio, ? AS rs_momentum",
                ("2454", None, 100.0),
            ).fetchone(),
        ]
        total, rank_map = _compute_rrg_universe_rank(rows)
        self.assertEqual(total, 1)
        self.assertIn("2330", rank_map)
        self.assertNotIn("2454", rank_map)
        conn.close()

    def test_build_badges_json_new_observation(self) -> None:
        row = LensRow(
            trade_date="2026-06-22",
            stock_id="2330",
            delta_new_to_watchlist=True,
        )
        badges = build_badges_json(row)
        self.assertEqual(badges[0]["key"], "new_observation")
        self.assertEqual(badges[0]["label_zh"], "新進觀察")
        self.assertEqual(badges[0]["plain_zh"], "新進觀察")

    def test_apply_featured_ranks_orders_by_strategy_then_score(self) -> None:
        rows = [
            LensRow(
                trade_date="2026-06-22",
                stock_id="A",
                lens_score=90,
                copytrade_l1h9_signal=True,
            ),
            LensRow(
                trade_date="2026-06-22",
                stock_id="B",
                lens_score=50,
                rrg_quadrant="leading",
                rrg_mono_fresh=True,
            ),
            LensRow(
                trade_date="2026-06-22",
                stock_id="C",
                lens_score=99,
                copytrade_l1h9_signal=True,
            ),
        ]
        _apply_featured_ranks(rows)
        self.assertEqual(rows[1].featured_rank, 1)
        self.assertEqual(rows[1].strategy_group_rank, 0)
        self.assertEqual(rows[2].featured_rank, 2)
        self.assertEqual(rows[2].strategy_group_rank, 2)
        self.assertEqual(rows[0].featured_rank, 3)
        self.assertEqual(rows[0].strategy_group_rank, 2)
        self.assertIsNotNone(rows[0].badges_json)

    def test_apply_featured_ranks_home_preview_positive_delta(self) -> None:
        rows = [
            LensRow(
                trade_date="2026-06-22",
                stock_id="2330",
                lens_score=80,
                delta_new_to_watchlist=True,
            ),
            LensRow(
                trade_date="2026-06-22",
                stock_id="2454",
                lens_score=90,
                narrative_zh="減碼",
            ),
        ]
        _apply_featured_ranks(rows)
        self.assertEqual(rows[0].home_preview_rank, 1)
        self.assertIsNone(rows[1].home_preview_rank)


if __name__ == "__main__":
    unittest.main()
