"""strategy_performance_yearly · SQLite + compute."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from strategy_performance_yearly import (
    StrategyPerformanceRow,
    ensure_strategy_performance_table,
    load_strategy_performance,
    upsert_strategy_performance,
)


class TestStrategyPerformanceYearly(unittest.TestCase):
    def test_upsert_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = f"{tmp}/t.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                row = StrategyPerformanceRow(
                    strategy_id="rrg-mono-hold7",
                    year_label="2026",
                    window_start="2026-01-01",
                    window_end="2026-06-18",
                    capital_ntd=50_000.0,
                    n_slots=3,
                    hold_days=7,
                    total_return_pct=127.6,
                    cagr_pct=569.3,
                    win_rate_vs_bench_pct=59.0,
                    sharpe_ratio=6.88,
                    mean_excess_pct=7.0,
                    n_periods=39,
                    partial_year=True,
                    computed_at="2026-06-22T12:00:00+08:00",
                )
                n = upsert_strategy_performance(conn, [row])
                self.assertEqual(n, 1)
                loaded = load_strategy_performance(conn, strategy_id="rrg-mono-hold7")
                self.assertEqual(len(loaded), 1)
                self.assertAlmostEqual(loaded[0]["total_return_pct"], 127.6)
                self.assertEqual(loaded[0]["year_label"], "2026")

                row2 = replace(row, total_return_pct=130.0, computed_at="2026-06-22T13:00:00+08:00")
                upsert_strategy_performance(conn, [row2])
                loaded2 = load_strategy_performance(conn, strategy_id="rrg-mono-hold7")
                self.assertAlmostEqual(loaded2[0]["total_return_pct"], 130.0)
            finally:
                conn.close()

    @patch("strategy_performance_yearly.sync_strategy_performance_to_supabase")
    @patch("strategy_performance_yearly.compute_strategy_performance_yearly")
    def test_refresh_calls_sqlite_and_supabase(
        self, mock_compute: object, mock_sync: object
    ) -> None:
        from strategy_performance_yearly import refresh_strategy_performance

        rows = [
            StrategyPerformanceRow(
                strategy_id="vcp-pivot-gate",
                year_label="2025",
                window_start="2025-01-01",
                window_end="2025-12-31",
                capital_ntd=50_000.0,
                total_return_pct=66.3,
                cagr_pct=69.4,
                win_rate_vs_bench_pct=52.2,
                sharpe_ratio=2.67,
                mean_excess_pct=5.26,
                n_periods=46,
                computed_at="2026-06-22T12:00:00+08:00",
            )
        ]
        mock_compute.return_value = rows
        mock_sync.return_value = ["vcp-pivot-gate:2025"]

        with tempfile.TemporaryDirectory() as tmp:
            db = f"{tmp}/t.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                out_rows, uploaded = refresh_strategy_performance(conn, sync_supabase=True)
                self.assertEqual(len(out_rows), 1)
                self.assertEqual(uploaded, ["vcp-pivot-gate:2025"])
                self.assertEqual(len(load_strategy_performance(conn)), 1)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
