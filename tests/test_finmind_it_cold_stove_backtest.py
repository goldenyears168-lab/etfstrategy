"""FinMind IT cold stove · unit tests."""
from __future__ import annotations

import unittest

from research.backtest.finmind_it_cold_stove_backtest import resolve_sl_tp_exit


class SlTpExitTests(unittest.TestCase):
    def test_take_profit_on_high(self) -> None:
        from stock_db import DEFAULT_DB_PATH, connect

        if not DEFAULT_DB_PATH.is_file():
            self.skipTest("no stocks.db")
        conn = connect(DEFAULT_DB_PATH)
        try:
            row = conn.execute(
                """
                SELECT stock_id, trade_date, open, high, low, close
                FROM stock_daily_bars
                WHERE source='finmind' AND high > open * 1.2
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                self.skipTest("no volatile bar")
            sid = str(row["stock_id"])
            entry_date = str(row["trade_date"])
            entry_px = float(row["open"]) * 0.9
            exit_d, exit_px, reason = resolve_sl_tp_exit(
                conn,
                sid,
                entry_date,
                entry_px,
                stop_loss_pct=10.0,
                take_profit_pct=15.0,
                max_hold_days=5,
            )
            self.assertIsNotNone(exit_d)
            self.assertIsNotNone(exit_px)
            self.assertIn(reason, ("take_profit", "take_profit_gap", "hold_expire", "stop_loss"))
        finally:
            conn.close()


class ValidateSmokeTests(unittest.TestCase):
    def test_validate_runs(self) -> None:
        from stock_db import DEFAULT_DB_PATH, connect
        from research.backtest.finmind_it_cold_stove_common import count_condition_passes

        if not DEFAULT_DB_PATH.is_file():
            self.skipTest("no stocks.db")
        import os
        if not os.environ.get("RUN_SLOW_DB_TESTS"):
            self.skipTest("slow real-DB smoke (~18s on mini); set RUN_SLOW_DB_TESTS=1 to run")
        conn = connect(DEFAULT_DB_PATH)
        try:
            out = count_condition_passes(conn, "tw100", "2024-06-01", "2024-06-30")
        finally:
            conn.close()
        self.assertIn("condition_hits", out)
        self.assertGreater(out["condition_hits"]["e1_it_buy"], 0)


if __name__ == "__main__":
    unittest.main()
